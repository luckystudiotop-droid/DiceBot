"""
Telegram-бот для игры "Угадай число" (aiogram 3 + aiohttp для Render Web Service).

Полный монолитный файл main.py.
Слушает порт Render через aiohttp, чтобы обходить порт-сканер, и параллельно гоняет поллинг бота.
"""

import asyncio
from collections import Counter
from dataclasses import dataclass, field
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from aiohttp import web

# ---------------------------------------------------------------------------
# 1. Логика подсчёта очков
# ---------------------------------------------------------------------------

@dataclass
class RoundResult:
    user_id: int
    display_name: str
    answer: list[int]
    points: int
    reason: str


def _positional_matches(answer: list[int], roll: list[int]) -> int:
    """Сколько чисел совпало на своих позициях."""
    return sum(1 for a, r in zip(answer, roll) if a == r)


def _multiset_matches(answer: list[int], roll: list[int]) -> int:
    """Сколько чисел совпадает вообще (без учёта позиции)."""
    ans_counter = Counter(answer)
    roll_counter = Counter(roll)
    overlap = ans_counter & roll_counter
    return sum(overlap.values())


def score_round1(answer: list[int], roll: list[int]) -> tuple[int, str]:
    if len(answer) != 1 or len(roll) != 1:
        raise ValueError("Раунд 1 ожидает ровно 1 число в ответе и в броске")
    if answer[0] == roll[0]:
        return 1, "угадал число (+1)"
    return 0, "не угадал"


def score_round2(answer: list[int], roll: list[int]) -> tuple[int, str]:
    if len(answer) != 2 or len(roll) != 2:
        raise ValueError("Раунд 2 ожидает ровно 2 числа в ответе и в броске")

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
    if len(answer) != 3 or len(roll) != 3:
        raise ValueError("Раунд 3 ожидает ровно 3 числа в ответе и в броске")

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


SCORERS = {
    1: score_round1,
    2: score_round2,
    3: score_round3,
}


def score_answer(round_number: int, answer: list[int], roll: list[int]) -> tuple[int, str]:
    scorer = SCORERS.get(round_number)
    if scorer is None:
        raise ValueError(f"Неизвестный раунд: {round_number}")
    return scorer(answer, roll)


def parse_numbers(text: str, expected_count: int) -> list[int] | None:
    cleaned = text.replace(",", " ").split()
    if len(cleaned) != expected_count:
        return None

    numbers = []
    for token in cleaned:
        if not token.isdigit():
            return None
        value = int(token)
        if not (1 <= value <= 6):
            return None
        numbers.append(value)

    return numbers


# ---------------------------------------------------------------------------
# 2. Состояние игровой сессии
# ---------------------------------------------------------------------------

@dataclass
class GameState:
    current_round: int | None = None
    accepting_answers: bool = False
    roll_pending: bool = False
    last_roll: list[int] | None = None
    pending_answers: dict[int, tuple[str, str]] = field(default_factory=dict)
    scores: dict[int, tuple[str, int]] = field(default_factory=dict)

    def start_new_round(self, round_number: int) -> None:
        self.current_round = round_number
        self.accepting_answers = True
        self.roll_pending = False
        self.last_roll = None
        self.pending_answers = {}

    def set_last_roll(self, roll: list[int]) -> None:
        self.last_roll = roll
        self.roll_pending = True
        self.accepting_answers = False

    def finish_check(self) -> None:
        self.pending_answers = {}
        self.last_roll = None
        self.roll_pending = False
        self.accepting_answers = True

    def add_score(self, user_id: int, name: str, points: int) -> None:
        _, existing = self.scores.get(user_id, (name, 0))
        self.scores[user_id] = (name, existing + points)

    def get_scoreboard(self) -> list[tuple[str, int]]:
        table = [(name, score) for name, score in self.scores.values()]
        table.sort(key=lambda row: row[1], reverse=True)
        return table


# ---------------------------------------------------------------------------
# 3. Настройка бота и хэндлеров
# ---------------------------------------------------------------------------

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

games: dict[int, GameState] = {}


def get_game(chat_id: int) -> GameState:
    if chat_id not in games:
        games[chat_id] = GameState()
    return games[chat_id]


def display_name(message: Message) -> str:
    user = message.from_user
    if user.username:
        return f"@{user.username}"
    return user.full_name


@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer("бот работает 🎲")


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    text = (
        "<b>Команды бота «Угадай число»</b>\n\n"
        "/start — проверить, что бот на связи\n"
        "/help — список всех команд (эта справка)\n\n"
        "<b>Управление игрой:</b>\n"
        "/startgame — начать новую игровую сессию (очки обнуляются)\n"
        "/round 1|2|3 — объявить текущий раунд и открыть приём ответов\n"
        "/roll — бросить кубики и закрыть приём ответов\n"
        "/check — сверить ответы с броском, начислить очки, показать результаты крутки\n"
        "/scores — показать текущую турнирную таблицу\n"
        "/endgame — завершить игру и показать финальный зачёт\n\n"
        "<b>Как играть:</b>\n"
        "1. /startgame\n"
        "2. /round 1 (или 2, или 3)\n"
        "3. Игроки пишут числа от 1 до 6 через пробел\n"
        "4. /roll — бот кидает кубики\n"
        "5. /check — бот считает очки за эту крутку\n"
        "6. Повторяем шаги 3-5 нужное число раз в рамках раунда\n"
        "7. При смене раунда — снова /round N\n"
        "8. /endgame — когда игра полностью закончена"
    )
    await message.answer(text)


@dp.message(Command("startgame"))
async def cmd_startgame(message: Message) -> None:
    chat_id = message.chat.id
    games[chat_id] = GameState()
    await message.answer(
        "🎮 Новая игра начата, очки обнулены.\n"
        "Объявите раунд командой /round 1 (или 2, или 3)."
    )


@dp.message(Command("round"))
async def cmd_round(message: Message, command: CommandObject) -> None:
    game = get_game(message.chat.id)

    if not command.args or not command.args.strip().isdigit():
        await message.answer("Укажите номер раунда: /round 1, /round 2 или /round 3")
        return

    round_number = int(command.args.strip())
    if round_number not in (1, 2, 3):
        await message.answer("Раунд должен быть 1, 2 или 3.")
        return

    game.start_new_round(round_number)

    rules = {
        1: "Пишите 1 число от 1 до 6.\nУгадал -> +1",
        2: (
            "Пишите 2 числа от 1 до 6 через пробел (например: 3 4).\n"
            "2 из 2 в правильном порядке -> +5\n"
            "2 из 2 в любом порядке -> +2\n"
            "1 из 2 -> +1"
        ),
        3: (
            "Пишите 3 числа от 1 до 6 через пробел (например: 1 2 3).\n"
            "3 из 3 в правильном порядке -> +10\n"
            "2 из 3 в правильном порядке -> +5\n"
            "3 из 3 в любом порядке -> +3\n"
            "2 из 3 в любом порядке -> +2\n"
            "1 из 3 -> +1"
        ),
    }

    await message.answer(
        f"📢 <b>Раунд {round_number}</b> начат.\n\n{rules[round_number]}\n\n"
        f"Приём ответов открыт. Когда все написали — жду /roll"
    )


@dp.message(Command("roll"))
async def cmd_roll(message: Message) -> None:
    chat_id = message.chat.id
    game = get_game(chat_id)

    if game.current_round is None:
        await message.answer("Сначала объявите раунд: /round 1, /round 2 или /round 3")
        return

    if game.roll_pending:
        await message.answer(
            "Предыдущий бросок ещё не сверен командой /check. "
            "Сначала выполните /check, потом бросайте заново."
        )
        return

    n = game.current_round
    roll_values: list[int] = []

    for i in range(n):
        dice_message = await bot.send_dice(chat_id, emoji="🎲")
        roll_values.append(dice_message.dice.value)
        if i < n - 1:
            await asyncio.sleep(4)

    game.set_last_roll(roll_values)

    await message.answer(
        f"🎲 Результат броска: <b>{' '.join(map(str, roll_values))}</b>\n\n"
        f"Теперь можно вводить /check для подсчёта очков."
    )


@dp.message(Command("check"))
async def cmd_check(message: Message) -> None:
    chat_id = message.chat.id
    game = get_game(chat_id)

    if game.current_round is None:
        await message.answer("Раунд не объявлен. Используйте /round 1|2|3")
        return

    if game.last_roll is None:
        await message.answer("Ещё не было броска в этой крутке. Сначала /roll")
        return

    round_number = game.current_round
    roll = game.last_roll
    expected_count = round_number

    results_lines = []
    invalid_lines = []

    for user_id, (name, raw_text) in game.pending_answers.items():
        parsed = parse_numbers(raw_text, expected_count)
        if parsed is None:
            invalid_lines.append(f"⚠️ {name}: «{raw_text}» — не распознано, 0 очков")
            continue

        points, reason = score_answer(round_number, parsed, roll)
        game.add_score(user_id, name, points)
        results_lines.append(f"{name}: {' '.join(map(str, parsed))} — {reason}")

    game.finish_check()

    if not results_lines and not invalid_lines:
        await message.answer("Никто не успел ответить в этой крутке. Очки не начислены.")
        return

    reply = f"🎯 Бросок был: <b>{' '.join(map(str, roll))}</b>\n\n"
    if results_lines:
        reply += "\n".join(results_lines)
    if invalid_lines:
        reply += "\n\n" + "\n".join(invalid_lines)

    await message.answer(reply)


@dp.message(Command("scores"))
async def cmd_scores(message: Message) -> None:
    game = get_game(message.chat.id)
    table = game.get_scoreboard()

    if not table:
        await message.answer("Пока никто не набрал очков.")
        return

    lines = ["🏆 <b>Турнирная таблица</b>\n"]
    for i, (name, score) in enumerate(table, start=1):
        lines.append(f"{i}. {name} — {score}")

    await message.answer("\n".join(lines))


@dp.message(Command("endgame"))
async def cmd_endgame(message: Message) -> None:
    chat_id = message.chat.id
    game = get_game(chat_id)
    table = game.get_scoreboard()

    if not table:
        await message.answer("Игра завершена. Очков никто не набрал.")
    else:
        lines = ["🏁 <b>Игра завершена! Финальный зачёт:</b>\n"]
        medals = ["🥇", "🥈", "🥉"]
        for i, (name, score) in enumerate(table):
            prefix = medals[i] if i < 3 else f"{i + 1}."
            lines.append(f"{prefix} {name} — {score}")
        await message.answer("\n".join(lines))

    del games[chat_id]


@dp.message(F.text & ~F.text.startswith("/"))
async def collect_answer(message: Message) -> None:
    chat_id = message.chat.id
    game = get_game(chat_id)

    if game.current_round is None or not game.accepting_answers:
        return

    text = message.text.strip()
    expected_count = game.current_round

    parsed = parse_numbers(text, expected_count)
    if parsed is None:
        return

    user_id = message.from_user.id
    name = display_name(message)

    if user_id not in game.pending_answers:
        game.pending_answers[user_id] = (name, text)


# ---------------------------------------------------------------------------
# 4. Фейковый HTTP-сервер для Render Web Service + Точка входа
# ---------------------------------------------------------------------------

async def handle_ping(request: web.Request) -> web.Response:
    """Отвечает 200 OK на проверки здоровья Render."""
    return web.Response(text="OK")


async def main() -> None:
    logger.info("Бот запускается...")

    # Поднимаем веб-сервер на порту, который требует Render
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Фейковый веб-сервер успешно запущен на порту {port}")

    # Запускаем поллинг aiogram
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())