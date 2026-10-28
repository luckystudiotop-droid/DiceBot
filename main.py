"""
DiceBot — Telegram-бот для игры «Угадай число» (aiogram 3 + aiohttp для Render Web Service).

Монолитный main.py:
- команды и кнопки доступны только администраторам чата;
- /settings — меню настроек (автоподсчёт очков после /roll);
- /round N открывает раунд, дальше крутки идут через /next или кнопку «СЛЕДУЮЩАЯ КРУТКА»;
- aiohttp-сервер слушает порт Render (health-check для UptimeRobot: /health).
"""

import asyncio
import html
import logging
import os
import time
from collections import Counter
from contextlib import suppress
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Awaitable, Callable

from aiohttp import web
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeDefault,
    CallbackQuery,
    Chat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
    User,
)

# ---------------------------------------------------------------------------
# 0. Конфигурация
# ---------------------------------------------------------------------------

DICE_DELAY_OPTIONS = (0, 1, 2, 3, 4, 5)  # варианты задержки анимации кубиков в настройках, сек (0 = выкл)
DICE_DELAY_DEFAULT = 4
ADMIN_CACHE_TTL = 60          # кэш списка админов чата, сек
DENY_COOLDOWN_SEC = 10        # не чаще одного «нет доступа» на юзера
DENY_AUTODELETE_SEC = 8       # через сколько удалять команду не-админа и ответ бота

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError(
        "Не найден BOT_TOKEN. Задайте переменную окружения BOT_TOKEN "
        "(в Render: Environment -> Add Environment Variable)."
    )

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# ---------------------------------------------------------------------------
# 1. Логика подсчёта очков
# ---------------------------------------------------------------------------


def _positional_matches(answer: list[int], roll: list[int]) -> int:
    """Сколько чисел совпало на своих позициях."""
    return sum(1 for a, r in zip(answer, roll) if a == r)


def _multiset_matches(answer: list[int], roll: list[int]) -> int:
    """Сколько чисел совпадает вообще (без учёта позиции)."""
    return sum((Counter(answer) & Counter(roll)).values())


def score_round1(answer: list[int], roll: list[int]) -> tuple[int, str]:
    if answer[0] == roll[0]:
        return 1, "угадал число (+1)"
    return 0, "не угадал"


def score_round2(answer: list[int], roll: list[int]) -> tuple[int, str]:
    positional = _positional_matches(answer, roll)
    multiset = _multiset_matches(answer, roll)
    if positional == 2:
        return 5, "2 из 2 в правильном порядке (+5)"
    if multiset == 2:
        return 2, "2 из 2, порядок не совпал (+2)"
    if multiset == 1:
        return 1, "1 из 2 (+1)"
    return 0, "не угадал"


def score_round3(answer: list[int], roll: list[int]) -> tuple[int, str]:
    positional = _positional_matches(answer, roll)
    multiset = _multiset_matches(answer, roll)
    if positional == 3:
        return 10, "3 из 3 в правильном порядке (+10)"
    if positional == 2:
        return 5, "2 из 3 в правильном порядке (+5)"
    if multiset == 3:
        return 3, "3 из 3, порядок не совпал (+3)"
    if multiset == 2:
        return 2, "2 из 3, порядок не совпал (+2)"
    if multiset == 1:
        return 1, "1 из 3 (+1)"
    return 0, "не угадал"


SCORERS = {1: score_round1, 2: score_round2, 3: score_round3}


def score_answer(round_number: int, answer: list[int], roll: list[int]) -> tuple[int, str]:
    if len(answer) != round_number or len(roll) != round_number:
        raise ValueError(f"Раунд {round_number}: неверное количество чисел")
    return SCORERS[round_number](answer, roll)


def parse_numbers(text: str, expected_count: int) -> list[int] | None:
    tokens = text.replace(",", " ").split()
    if len(tokens) != expected_count:
        return None
    numbers = []
    for token in tokens:
        # isascii() отсекает «²», «①» и т.п. — у них isdigit()=True, но int() падает
        if not (token.isascii() and token.isdigit()):
            return None
        value = int(token)
        if not 1 <= value <= 6:
            return None
        numbers.append(value)
    return numbers


# ---------------------------------------------------------------------------
# 2. Состояние игры и настройки
# ---------------------------------------------------------------------------


class Phase(Enum):
    IDLE = auto()        # раунд не объявлен
    COLLECTING = auto()  # крутка открыта, принимаем ответы
    ROLLING = auto()     # кубики летят, ответы закрыты
    ROLLED = auto()      # бросок есть, ждём /check (ручной режим)
    CHECKED = auto()     # крутка посчитана, ждём /next или /round


@dataclass
class GameState:
    round_number: int | None = None
    spin: int = 0
    phase: Phase = Phase.IDLE
    last_roll: list[int] | None = None
    answers: dict[int, tuple[str, list[int]]] = field(default_factory=dict)
    scores: dict[int, tuple[str, int]] = field(default_factory=dict)
    button_msg: tuple[int, int] | None = None  # (chat_id, message_id) сообщения с кнопкой

    def start_round(self, round_number: int) -> None:
        self.round_number = round_number
        self.spin = 1
        self._open_spin()

    def next_spin(self) -> None:
        self.spin += 1
        self._open_spin()

    def _open_spin(self) -> None:
        self.phase = Phase.COLLECTING
        self.last_roll = None
        self.answers = {}

    def add_score(self, user_id: int, name: str, points: int) -> None:
        _, existing = self.scores.get(user_id, (name, 0))
        self.scores[user_id] = (name, existing + points)

    def get_scoreboard(self) -> list[tuple[str, int]]:
        return sorted(self.scores.values(), key=lambda row: row[1], reverse=True)


@dataclass
class ChatSettings:
    auto_check: bool = True
    dice_delay: int = DICE_DELAY_DEFAULT  # 0 = без задержки


games: dict[int, GameState] = {}
chat_settings: dict[int, ChatSettings] = {}


def get_game(chat_id: int) -> GameState:
    return games.setdefault(chat_id, GameState())


def get_settings(chat_id: int) -> ChatSettings:
    return chat_settings.setdefault(chat_id, ChatSettings())


# ---------------------------------------------------------------------------
# 3. Утилиты
# ---------------------------------------------------------------------------

_bg_tasks: set[asyncio.Task] = set()


def run_bg(coro: Awaitable[Any]) -> None:
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def delete_later(delay: float, *messages: Message) -> None:
    await asyncio.sleep(delay)
    for msg in messages:
        with suppress(TelegramAPIError):
            await msg.delete()


def display_name(user: User) -> str:
    return f"@{user.username}" if user.username else user.full_name


def esc(text: str) -> str:
    return html.escape(text, quote=False)


def fmt(numbers: list[int]) -> str:
    return " ".join(map(str, numbers))


def numbers_word(n: int) -> str:
    return "число" if n == 1 else "числа"


RULES = {
    1: "Пишите 1 число от 1 до 6.\nУгадал → +1",
    2: (
        "Пишите 2 числа от 1 до 6 через пробел (например: 3 4).\n"
        "2 из 2 в правильном порядке → +5\n"
        "2 из 2 в любом порядке → +2\n"
        "1 из 2 → +1"
    ),
    3: (
        "Пишите 3 числа от 1 до 6 через пробел (например: 1 2 3).\n"
        "3 из 3 в правильном порядке → +10\n"
        "2 из 3 в правильном порядке → +5\n"
        "3 из 3 в любом порядке → +3\n"
        "2 из 3 в любом порядке → +2\n"
        "1 из 3 → +1"
    ),
}

MSG_ROLLING = "🎲 Кубики ещё летят — подождите пару секунд."
MSG_UNCHECKED = "⚠️ Есть несверенный бросок. Сначала посчитайте его: /check"


def busy_error(game: GameState) -> str | None:
    """Ошибка, если сейчас нельзя менять раунд/игру."""
    if game.phase is Phase.ROLLING:
        return MSG_ROLLING
    if game.phase is Phase.ROLLED:
        return MSG_UNCHECKED
    return None


def next_spin_error(game: GameState) -> str | None:
    if game.phase is Phase.IDLE:
        return "Раунд не объявлен. Начните: /round 1, /round 2 или /round 3"
    if game.phase is Phase.COLLECTING:
        return f"Крутка №{game.spin} уже открыта — жду /roll"
    return busy_error(game)


# ---------------------------------------------------------------------------
# 4. Доступ только для админов
# ---------------------------------------------------------------------------

DENY_ALERT = "⛔ Доступ закрыт\n\nУправлять игрой могут только администраторы чата 👑"

_admin_cache: dict[int, tuple[float, set[int]]] = {}
_deny_cooldown: dict[tuple[int, int], float] = {}


async def is_chat_admin(chat: Chat, user_id: int | None, sender_chat: Chat | None = None) -> bool:
    if chat.type == ChatType.PRIVATE:
        return True  # в личке пользователь сам себе хозяин
    if sender_chat is not None and sender_chat.id == chat.id:
        return True  # анонимный администратор
    if user_id is None:
        return False

    now = time.monotonic()
    cached = _admin_cache.get(chat.id)
    if cached is None or now - cached[0] > ADMIN_CACHE_TTL:
        try:
            admins = await bot.get_chat_administrators(chat.id)
        except TelegramAPIError:
            logger.exception("Не удалось получить список админов чата %s", chat.id)
            return False
        cached = (now, {member.user.id for member in admins})
        _admin_cache[chat.id] = cached
    return user_id in cached[1]


async def deny_in_chat(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else 0
    key = (message.chat.id, user_id)
    now = time.monotonic()

    if now - _deny_cooldown.get(key, 0) < DENY_COOLDOWN_SEC:
        with suppress(TelegramAPIError):
            await message.delete()
        return
    _deny_cooldown[key] = now

    name = esc(message.from_user.full_name) if message.from_user else "Участник"
    try:
        reply = await message.reply(
            "⛔ <b>ДОСТУП ТОЛЬКО ДЛЯ АДМИНОВ</b>\n\n"
            f"{name}, команды бота доступны только администраторам чата 👑"
        )
    except TelegramAPIError:
        return
    run_bg(delete_later(DENY_AUTODELETE_SEC, message, reply))


class AdminCommandsMiddleware(BaseMiddleware):
    """Inner-middleware: срабатывает только когда команда реально адресована этому боту."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message) or not (event.text or "").startswith("/"):
            return await handler(event, data)
        user_id = event.from_user.id if event.from_user else None
        if await is_chat_admin(event.chat, user_id, event.sender_chat):
            return await handler(event, data)
        await deny_in_chat(event)
        return None


class AdminCallbacksMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, CallbackQuery):
            return await handler(event, data)
        chat = event.message.chat if event.message else None
        if chat is not None and await is_chat_admin(chat, event.from_user.id):
            return await handler(event, data)
        await event.answer(DENY_ALERT, show_alert=True)
        return None


dp.message.middleware(AdminCommandsMiddleware())
dp.callback_query.middleware(AdminCallbacksMiddleware())

# ---------------------------------------------------------------------------
# 5. Клавиатуры
# ---------------------------------------------------------------------------

CB_NEXT = "game:next"
CB_TOGGLE_AUTO = "settings:auto_check"
CB_SETTINGS_CLOSE = "settings:close"
CB_DELAY_PREFIX = "settings:delay:"


def next_spin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="▶️ СЛЕДУЮЩАЯ КРУТКА", callback_data=CB_NEXT)]]
    )


def delay_label(seconds: int) -> str:
    return "ВЫКЛ" if seconds == 0 else f"{seconds} сек"


def settings_text(s: ChatSettings) -> str:
    delay_hint = (
        "кубики летят сразу один за другим, результат приходит без ожидания"
        if s.dice_delay == 0
        else f"бот ждёт {s.dice_delay} сек после каждого кубика, чтобы анимация докрутилась"
    )
    mode = (
        "после /roll бот сам сверяет ответы и начисляет очки"
        if s.auto_check
        else "после /roll бот ждёт команду /check от админа"
    )
    return (
        "⚙️ <b>Настройки игры</b>\n\n"
        f"🤖 Автоподсчёт очков: <b>{'ВКЛ' if s.auto_check else 'ВЫКЛ'}</b>\n"
        f"<i>Сейчас {mode}.</i>\n\n"
        f"⏱ Задержка анимации кубиков: <b>{delay_label(s.dice_delay)}</b>\n"
        f"<i>{delay_hint}</i>"
    )


def settings_kb(s: ChatSettings) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text=f"🤖 Автоподсчёт: {'✅ ВКЛ' if s.auto_check else '❌ ВЫКЛ'}",
                callback_data=CB_TOGGLE_AUTO,
            )],
            [InlineKeyboardButton(text="⏱ Задержка анимации кубиков:", callback_data=CB_DELAY_PREFIX + "info")],
            [
                InlineKeyboardButton(
                    text=("✅ " if s.dice_delay == sec else "") + ("Выкл" if sec == 0 else f"{sec}с"),
                    callback_data=f"{CB_DELAY_PREFIX}{sec}",
                )
                for sec in DICE_DELAY_OPTIONS
            ],
            [InlineKeyboardButton(text="✖️ Закрыть", callback_data=CB_SETTINGS_CLOSE)],
        ]
    )


async def drop_next_button(game: GameState) -> None:
    """Убирает кнопку «Следующая крутка» с последнего сообщения с результатами."""
    if game.button_msg is None:
        return
    chat_id, message_id = game.button_msg
    game.button_msg = None
    with suppress(TelegramAPIError):
        await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)


# ---------------------------------------------------------------------------
# 6. Игровые действия
# ---------------------------------------------------------------------------


async def announce_spin(message: Message, game: GameState, first: bool) -> None:
    n = game.round_number
    if first:
        text = (
            f"📢 <b>Раунд {n}</b> начат.\n\n{RULES[n]}\n\n"
            f"🔄 Крутка №1 — приём ответов открыт.\nКогда все написали — жду /roll"
        )
    else:
        spacing = " через пробел" if n > 1 else ""
        text = (
            f"🔄 <b>Раунд {n} · Крутка №{game.spin}</b>\n\n"
            f"Пишите {n} {numbers_word(n)} от 1 до 6{spacing}.\n"
            f"Приём ответов открыт — когда все написали, жду /roll"
        )
    await message.answer(text)


async def send_dice_safe(message: Message) -> Message:
    for _ in range(2):
        try:
            return await message.answer_dice(emoji="🎲")
        except TelegramRetryAfter as e:
            logger.warning("Flood control, ждём %s сек", e.retry_after)
            await asyncio.sleep(e.retry_after + 1)
    return await message.answer_dice(emoji="🎲")


async def do_roll(message: Message, game: GameState) -> None:
    n = game.round_number
    game.phase = Phase.ROLLING  # синхронно, до первого await: ответы закрыты, повторный /roll отбивается

    delay = get_settings(message.chat.id).dice_delay
    values: list[int] = []
    try:
        await message.answer(
            f"🔒 Приём ответов закрыт. Принято ответов: <b>{len(game.answers)}</b>\n🎲 Бросаю..."
        )
        for _ in range(n):
            dice_msg = await send_dice_safe(message)
            values.append(dice_msg.dice.value)
            if delay:
                await asyncio.sleep(delay)  # ждём, пока докрутится анимация
    except Exception:
        logger.exception("Ошибка при броске кубиков")
        game.phase = Phase.COLLECTING  # не оставляем игру навсегда в состоянии «бросаю»
        with suppress(TelegramAPIError):
            await message.answer("⚠️ Не удалось бросить кубики (ошибка связи). Ответы сохранены — повторите /roll")
        return

    game.last_roll = values
    game.phase = Phase.ROLLED

    if get_settings(message.chat.id).auto_check:
        await do_check(message, game)
    else:
        await message.answer(f"🎲 Результат броска: <b>{fmt(values)}</b>\n\nДля подсчёта очков — /check")


async def do_check(message: Message, game: GameState) -> None:
    roll = game.last_roll or []
    n = game.round_number

    results: list[tuple[int, str]] = []
    for user_id, (name, numbers) in game.answers.items():
        points, reason = score_answer(n, numbers, roll)
        game.add_score(user_id, name, points)
        results.append((points, f"{esc(name)}: {fmt(numbers)} — {reason}"))

    game.answers = {}
    game.phase = Phase.CHECKED

    results.sort(key=lambda r: r[0], reverse=True)
    body = "\n".join(line for _, line in results) if results else "Никто не ответил в этой крутке — очки не начислены."

    sent = await message.answer(
        f"🎯 Бросок был: <b>{fmt(roll)}</b>\n<i>Раунд {n} · крутка №{game.spin}</i>\n\n{body}",
        reply_markup=next_spin_kb(),
    )
    game.button_msg = (sent.chat.id, sent.message_id)


# ---------------------------------------------------------------------------
# 7. Хэндлеры команд
# ---------------------------------------------------------------------------


@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer("✅ Бот работает. Список команд — /help")


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "<b>🎲 Команды бота «Угадай число»</b>\n"
        "<i>Все команды доступны только администраторам чата.</i>\n\n"
        "/start — проверить, что бот на связи\n"
        "/help — эта справка\n"
        "/settings — настройки (автоподсчёт очков, задержка анимации кубиков)\n\n"
        "<b>Управление игрой:</b>\n"
        "/startgame — новая игра (очки обнуляются)\n"
        "/round 1|2|3 — начать раунд и открыть первую крутку\n"
        "/roll — закрыть приём ответов и бросить кубики\n"
        "/check — посчитать очки за крутку (если автоподсчёт выключен)\n"
        "/next — следующая крутка того же раунда (или кнопка ▶️ под результатами)\n"
        "/scores — турнирная таблица\n"
        "/endgame — завершить игру и показать финальный зачёт\n\n"
        "<b>Как играть:</b>\n"
        "1. /startgame\n"
        "2. /round 1 (или 2, или 3)\n"
        "3. Игроки пишут числа от 1 до 6 через пробел (засчитывается первый ответ)\n"
        "4. /roll — бот бросает кубики и сам считает очки\n"
        "   (если автоподсчёт выключен — дальше /check)\n"
        "5. ▶️ «Следующая крутка» или /next — и снова шаги 3–4\n"
        "6. Сменить раунд — /round N\n"
        "7. /endgame — когда игра закончена"
    )


@dp.message(Command("settings"))
async def cmd_settings(message: Message) -> None:
    s = get_settings(message.chat.id)
    await message.answer(settings_text(s), reply_markup=settings_kb(s))


@dp.message(Command("startgame"))
async def cmd_startgame(message: Message) -> None:
    old = games.get(message.chat.id)
    if old is not None and old.phase is Phase.ROLLING:
        await message.answer(MSG_ROLLING)
        return
    if old is not None:
        await drop_next_button(old)
    games[message.chat.id] = GameState()
    await message.answer(
        "🎮 Новая игра начата, очки обнулены.\nОбъявите раунд: /round 1, /round 2 или /round 3"
    )


@dp.message(Command("round"))
async def cmd_round(message: Message, command: CommandObject) -> None:
    game = get_game(message.chat.id)

    arg = (command.args or "").strip()
    if arg not in ("1", "2", "3"):
        await message.answer("Укажите номер раунда: /round 1, /round 2 или /round 3")
        return

    if err := busy_error(game):
        await message.answer(err)
        return

    game.start_round(int(arg))
    await drop_next_button(game)
    await announce_spin(message, game, first=True)


@dp.message(Command("roll"))
async def cmd_roll(message: Message) -> None:
    game = get_game(message.chat.id)

    if game.phase is Phase.IDLE:
        await message.answer("Сначала объявите раунд: /round 1, /round 2 или /round 3")
    elif game.phase is Phase.ROLLING:
        await message.answer(MSG_ROLLING)
    elif game.phase is Phase.ROLLED:
        await message.answer(MSG_UNCHECKED)
    elif game.phase is Phase.CHECKED:
        await message.answer("Эта крутка уже посчитана. Откройте следующую: /next или кнопка ▶️")
    else:
        await do_roll(message, game)


@dp.message(Command("check"))
async def cmd_check(message: Message) -> None:
    game = get_game(message.chat.id)

    if game.phase is Phase.IDLE:
        await message.answer("Раунд не объявлен. Используйте /round 1|2|3")
    elif game.phase is Phase.COLLECTING:
        await message.answer("В этой крутке ещё не было броска. Сначала /roll")
    elif game.phase is Phase.ROLLING:
        await message.answer(MSG_ROLLING)
    elif game.phase is Phase.CHECKED:
        await message.answer("Эта крутка уже посчитана. Следующая: /next или кнопка ▶️")
    else:
        await do_check(message, game)


@dp.message(Command("next"))
async def cmd_next(message: Message) -> None:
    game = get_game(message.chat.id)
    if err := next_spin_error(game):
        await message.answer(err)
        return
    game.next_spin()  # синхронно, до await — защита от двойного вызова
    await drop_next_button(game)
    await announce_spin(message, game, first=False)


@dp.message(Command("scores"))
async def cmd_scores(message: Message) -> None:
    table = get_game(message.chat.id).get_scoreboard()
    if not table:
        await message.answer("Пока никто не набрал очков.")
        return
    lines = ["🏆 <b>Турнирная таблица</b>\n"]
    lines += [f"{i}. {esc(name)} — {score}" for i, (name, score) in enumerate(table, start=1)]
    await message.answer("\n".join(lines))


@dp.message(Command("endgame"))
async def cmd_endgame(message: Message) -> None:
    chat_id = message.chat.id
    game = games.get(chat_id)
    if game is None:
        await message.answer("Игра не запущена. Начать — /startgame")
        return
    if err := busy_error(game):
        await message.answer(err)
        return

    await drop_next_button(game)
    table = game.get_scoreboard()
    games.pop(chat_id, None)

    if not table:
        await message.answer("🏁 Игра завершена. Очков никто не набрал.")
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏁 <b>Игра завершена! Финальный зачёт:</b>\n"]
    for i, (name, score) in enumerate(table):
        prefix = medals[i] if i < 3 else f"{i + 1}."
        lines.append(f"{prefix} {esc(name)} — {score}")
    await message.answer("\n".join(lines))


# ---------------------------------------------------------------------------
# 8. Кнопки
# ---------------------------------------------------------------------------


@dp.callback_query(F.data == CB_NEXT)
async def cb_next(callback: CallbackQuery) -> None:
    msg = callback.message
    if not isinstance(msg, Message):
        await callback.answer("Сообщение устарело — используйте /next", show_alert=True)
        return

    game = get_game(msg.chat.id)
    if err := next_spin_error(game):
        await callback.answer(err, show_alert=True)
        return

    game.next_spin()  # синхронно — двойное нажатие не откроет две крутки
    await drop_next_button(game)
    with suppress(TelegramAPIError):
        await msg.edit_reply_markup(reply_markup=None)  # на случай, если нажали на старое сообщение
    await announce_spin(msg, game, first=False)
    await callback.answer("▶️ Следующая крутка открыта")


@dp.callback_query(F.data == CB_TOGGLE_AUTO)
async def cb_toggle_auto(callback: CallbackQuery) -> None:
    msg = callback.message
    if not isinstance(msg, Message):
        await callback.answer("Откройте /settings заново", show_alert=True)
        return
    s = get_settings(msg.chat.id)
    s.auto_check = not s.auto_check
    with suppress(TelegramAPIError):
        await msg.edit_text(settings_text(s), reply_markup=settings_kb(s))
    await callback.answer("Автоподсчёт включён ✅" if s.auto_check else "Автоподсчёт выключен ❌")


@dp.callback_query(F.data.startswith(CB_DELAY_PREFIX))
async def cb_set_delay(callback: CallbackQuery) -> None:
    msg = callback.message
    if not isinstance(msg, Message):
        await callback.answer("Откройте /settings заново", show_alert=True)
        return
    value = callback.data.removeprefix(CB_DELAY_PREFIX)
    if not value.isdigit() or int(value) not in DICE_DELAY_OPTIONS:
        await callback.answer("Выберите значение кнопкой ниже 👇")
        return
    s = get_settings(msg.chat.id)
    if s.dice_delay == int(value):
        await callback.answer("Уже выбрано")
        return
    s.dice_delay = int(value)
    with suppress(TelegramAPIError):
        await msg.edit_text(settings_text(s), reply_markup=settings_kb(s))
    await callback.answer(f"Задержка анимации: {delay_label(s.dice_delay)}")


@dp.callback_query(F.data == CB_SETTINGS_CLOSE)
async def cb_settings_close(callback: CallbackQuery) -> None:
    if isinstance(callback.message, Message):
        with suppress(TelegramAPIError):
            await callback.message.delete()
    await callback.answer()


# ---------------------------------------------------------------------------
# 9. Сбор ответов игроков
# ---------------------------------------------------------------------------


@dp.message(F.text & ~F.text.startswith("/"))
async def collect_answer(message: Message) -> None:
    game = games.get(message.chat.id)
    if game is None or game.phase is not Phase.COLLECTING:
        return

    user = message.from_user
    if user is None or user.is_bot or message.sender_chat is not None:
        return
    if user.id in game.answers:
        return  # засчитывается только первый ответ

    parsed = parse_numbers(message.text, game.round_number)
    if parsed is None:
        return
    game.answers[user.id] = (display_name(user), parsed)


# ---------------------------------------------------------------------------
# 10. HTTP-сервер для Render + точка входа
# ---------------------------------------------------------------------------

BOT_COMMANDS = [
    BotCommand(command="startgame", description="Новая игра"),
    BotCommand(command="round", description="Начать раунд: /round 1|2|3"),
    BotCommand(command="roll", description="Бросить кубики"),
    BotCommand(command="next", description="Следующая крутка"),
    BotCommand(command="check", description="Посчитать очки (ручной режим)"),
    BotCommand(command="scores", description="Турнирная таблица"),
    BotCommand(command="endgame", description="Завершить игру"),
    BotCommand(command="settings", description="Настройки"),
    BotCommand(command="help", description="Справка"),
]


async def setup_bot_commands() -> None:
    """Меню команд по «/» видят только админы групп (и пользователь в личке)."""
    try:
        await bot.set_my_commands(BOT_COMMANDS, scope=BotCommandScopeAllChatAdministrators())
        await bot.set_my_commands(BOT_COMMANDS, scope=BotCommandScopeAllPrivateChats())
        await bot.delete_my_commands(scope=BotCommandScopeDefault())
    except TelegramAPIError:
        logger.exception("Не удалось установить меню команд")


async def handle_ping(request: web.Request) -> web.Response:
    return web.Response(text="OK")


async def main() -> None:
    logger.info("Бот запускается...")

    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logger.info("Веб-сервер запущен на порту %s", port)

    try:
        await setup_bot_commands()
        # После рестарта/сна старые команды не выполняем — состояние игры всё равно потеряно
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())