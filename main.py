import os
import asyncio
import logging
import sqlite3
import html
from contextlib import closing

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.exceptions import TelegramBadRequest
from openai import AsyncOpenAI

# ---------- Конфигурация ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_KEY = os.getenv("API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

BASE_URL = "https://api.proxyapi.ru/v1"

MODEL_TEXT_FAST = "openai/gpt-6.2-sol"
MODEL_TEXT_SMART = "anthropic/claude-sonnet-5.5"
MODEL_IMAGE = "openai/gpt-image-2.6-flare"

DB_PATH = "users.db"
TG_TEXT_LIMIT = 4000  # с запасом от 4096

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("ai-bot")

if not BOT_TOKEN or not API_KEY:
    raise SystemExit("BOT_TOKEN и API_KEY должны быть заданы в переменных окружения")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
ai_client = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL)

user_modes: dict[int, str] = {}
bot_username: str | None = None


# ---------- БД ----------
def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    with closing(get_conn()) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                text_balance INTEGER DEFAULT 5,
                img_balance INTEGER DEFAULT 1,
                referred_by INTEGER DEFAULT 0
            )
        """)
        conn.commit()


def get_user(user_id: int, ref_id: int = 0) -> tuple[tuple[int, int], bool]:
    """Возвращает ((text_balance, img_balance), is_new)."""
    with closing(get_conn()) as conn:
        cur = conn.execute(
            "SELECT text_balance, img_balance FROM users WHERE user_id = ?",
            (user_id,),
        )
        row = cur.fetchone()
        if row:
            return (row[0], row[1]), False

        # Проверяем, существует ли реферер, прежде чем начислять ему бонус
        valid_ref = 0
        if ref_id and ref_id != user_id:
            cur = conn.execute("SELECT 1 FROM users WHERE user_id = ?", (ref_id,))
            if cur.fetchone():
                valid_ref = ref_id

        conn.execute(
            "INSERT INTO users (user_id, text_balance, img_balance, referred_by) "
            "VALUES (?, 5, 1, ?)",
            (user_id, valid_ref),
        )
        if valid_ref:
            conn.execute(
                "UPDATE users SET text_balance = text_balance + 5, "
                "img_balance = img_balance + 1 WHERE user_id = ?",
                (valid_ref,),
            )
        conn.commit()
        return (5, 1), True


def update_balance(user_id: int, text_delta: int = 0, img_delta: int = 0) -> None:
    with closing(get_conn()) as conn:
        conn.execute(
            "UPDATE users SET text_balance = text_balance + ?, "
            "img_balance = img_balance + ? WHERE user_id = ?",
            (text_delta, img_delta, user_id),
        )
        conn.commit()


def try_consume(user_id: int, kind: str) -> bool:
    """Атомарно списывает 1 единицу баланса. True — если списано."""
    col = "text_balance" if kind == "text" else "img_balance"
    with closing(get_conn()) as conn:
        cur = conn.execute(
            f"UPDATE users SET {col} = {col} - 1 "
            f"WHERE user_id = ? AND {col} > 0",
            (user_id,),
        )
        conn.commit()
        return cur.rowcount > 0


def get_stats() -> tuple[int, int, int]:
    with closing(get_conn()) as conn:
        cur = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(text_balance), 0), "
            "COALESCE(SUM(img_balance), 0) FROM users"
        )
        return cur.fetchone()


# ---------- Клавиатуры ----------
main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="✍️ Написать / Решить задачу"),
         KeyboardButton(text="🎨 Создать картинку")],
        [KeyboardButton(text="🧠 Сложный анализ / Код"),
         KeyboardButton(text="👤 Мой профиль / Баланс")],
        [KeyboardButton(text="🎁 Бесплатные запросы"),
         KeyboardButton(text="💎 Купить подписку")],
    ],
    resize_keyboard=True,
)

tariffs_kb = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(
            text="Старт: 100 текстов + 10 картинок (290 ₽)",
            callback_data="buy_start")],
        [InlineKeyboardButton(
            text="Оптимальный: 300 текстов + 35 картинок (590 ₽)",
            callback_data="buy_opt")],
        [InlineKeyboardButton(
            text="Профи: 1000 текстов + 80 картинок (1190 ₽)",
            callback_data="buy_pro")],
    ]
)


# ---------- Утилиты ----------
def split_text(text: str, limit: int = TG_TEXT_LIMIT) -> list[str]:
    return [text[i:i + limit] for i in range(0, len(text), limit)] or [""]


async def safe_send_text(message: types.Message, text: str) -> None:
    """Отправляет длинный текст частями, без падения на markdown."""
    for chunk in split_text(text):
        try:
            await message.answer(chunk)
        except TelegramBadRequest as e:
            logger.warning("send_text failed (%s), retry plain", e)
            await message.answer(html.escape(chunk), parse_mode=None)


async def safe_delete(message: types.Message | None) -> None:
    if not message:
        return
    try:
        await message.delete()
    except TelegramBadRequest:
        pass


def is_admin(user_id: int) -> bool:
    return ADMIN_ID != 0 and user_id == ADMIN_ID


# ---------- Хендлеры ----------
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    args = message.text.split(maxsplit=1)
    ref_id = int(args[1]) if len(args) > 1 and args[1].isdigit() else 0

    (text_bal, img_bal), is_new = get_user(message.from_user.id, ref_id)
    user_modes[message.from_user.id] = "fast"

    if is_new and ref_id and ref_id != message.from_user.id:
        try:
            await bot.send_message(
                ref_id,
                "🎉 По твоей ссылке зашёл друг! "
                "Тебе начислено *+5 текстов* и *+1 картинка*.",
                parse_mode="Markdown",
            )
        except Exception:
            pass

    await message.answer(
        "👋 *Привет! Я твой универсальный ИИ-помощник.*\n\n"
        "Здесь собраны лучшие нейросети мира — "
        "*без зарубежных карт и сложных настроек*.\n\n"
        f"🎁 Твой баланс: *{text_bal} текстовых запросов* "
        f"и *{img_bal} генераций картинок*.\n\n"
        "👇 Выбери режим в меню ниже и просто напиши свой запрос:",
        reply_markup=main_kb,
        parse_mode="Markdown",
    )


@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if not is_admin(message.from_user.id):
        # Не палим наличие команды обычным юзерам
        return
    total_users, total_texts, total_imgs = get_stats()
    await message.answer(
        "👑 *Панель администратора*\n\n"
        f"👥 Всего пользователей: *{total_users}*\n"
        f"💬 Остаток текстов у юзеров: *{total_texts}*\n"
        f"🎨 Остаток картинок у юзеров: *{total_imgs}*\n\n"
        "➕ Начислить баланс вручную:\n"
        "`/add ID тексты картинки`\n"
        "Пример: `/add 123456789 100 10`",
        parse_mode="Markdown",
    )


@dp.message(Command("add"))
async def cmd_add_balance(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 4:
        await message.answer("⚠️ Формат: `/add ID тексты картинки`",
                             parse_mode="Markdown")
        return
    try:
        target_id, add_t, add_i = int(parts[1]), int(parts[2]), int(parts[3])
    except ValueError:
        await message.answer("⚠️ ID и числа должны быть целыми.")
        return

    get_user(target_id)  # создаём, если нет
    update_balance(target_id, text_delta=add_t, img_delta=add_i)
    await message.answer(
        f"✅ Пользователю `{target_id}` начислено "
        f"+{add_t} текстов и +{add_i} картинок.",
        parse_mode="Markdown",
    )
    try:
        await bot.send_message(
            target_id,
            f"🎁 Администратор начислил тебе "
            f"*+{add_t} текстов* и *+{add_i} картинок*!",
            parse_mode="Markdown",
        )
    except Exception:
        pass


@dp.message(F.text == "✍️ Написать / Решить задачу")
async def mode_fast(message: types.Message):
    user_modes[message.from_user.id] = "fast"
    await message.answer("✍️ Режим *Быстрый текст / Учеба* включён. "
                         "Напиши свой вопрос:", parse_mode="Markdown")


@dp.message(F.text == "🧠 Сложный анализ / Код")
async def mode_smart(message: types.Message):
    user_modes[message.from_user.id] = "smart"
    await message.answer("🧠 Режим *Глубокий анализ / Код (Claude)* включён. "
                         "Опиши задачу:", parse_mode="Markdown")


@dp.message(F.text == "🎨 Создать картинку")
async def mode_image(message: types.Message):
    (_, img_bal), _ = get_user(message.from_user.id)
    if img_bal <= 0:
        await message.answer(
            "❌ Закончились генерации картинок. "
            "Нажми «💎 Купить подписку» или «🎁 Бесплатные запросы».")
        return
    user_modes[message.from_user.id] = "image"
    await message.answer("🎨 Режим *Генерация картинок* включён. "
                         "Опиши, что нарисовать:", parse_mode="Markdown")


@dp.message(F.text == "👤 Мой профиль / Баланс")
async def show_profile(message: types.Message):
    (text_bal, img_bal), _ = get_user(message.from_user.id)
    await message.answer(
        f"👤 *Твой профиль (ID: `{message.from_user.id}`)*\n\n"
        f"💬 Остаток текстовых запросов: *{text_bal}*\n"
        f"🎨 Остаток картинок: *{img_bal}*",
        parse_mode="Markdown",
    )


@dp.message(F.text == "🎁 Бесплатные запросы")
async def show_referral(message: types.Message):
    global bot_username
    if bot_username is None:
        me = await bot.get_me()
        bot_username = me.username or ""
    ref_link = f"https://t.me/{bot_username}?start={message.from_user.id}"
    await message.answer(
        "🎁 *Получай запросы бесплатно!*\n\n"
        "Отправь свою личную ссылку друзьям. За каждого нового "
        "пользователя ты получишь *+5 текстов* и *+1 картинку*:\n\n"
        f"`{ref_link}`",
        parse_mode="Markdown",
    )


@dp.message(F.text == "💎 Купить подписку")
async def show_tariffs(message: types.Message):
    await message.answer(
        "💎 *Выбери подходящий пакет запросов:*\n\n"
        "Оплата по СБП и картами РФ.",
        reply_markup=tariffs_kb,
        parse_mode="Markdown",
    )


@dp.callback_query(F.data.startswith("buy_"))
async def process_buy(callback: types.CallbackQuery):
    try:
        await callback.message.answer(
            "💳 Для моментального пополнения напишите администратору "
            f"и укажите ваш ID: `{callback.from_user.id}`",
            parse_mode="Markdown",
        )
    finally:
        await callback.answer()


@dp.message(F.text & ~F.text.startswith("/"))
async def handle_ai_request(message: types.Message):
    user_id = message.from_user.id
    mode = user_modes.get(user_id, "fast")

    # --- Картинки ---
    if mode == "image":
        if not try_consume(user_id, "img"):
            await message.answer(
                "❌ Закончились генерации картинок. "
                "Нажми «💎 Купить подписку» или «🎁 Бесплатные запросы».")
            return

        wait_msg = await message.answer("🎨 Рисую картинку, подождите 10–15 секунд...")
        try:
            response = await ai_client.images.generate(
                model=MODEL_IMAGE,
                prompt=message.text,
                n=1,
                size="1024x1024",
            )
            image_url = next(iter(response.data)).url
            await safe_delete(wait_msg)
            (_, img_bal), _ = get_user(user_id)
            await message.answer_photo(
                photo=image_url,
                caption=f"✅ Готово! Осталось картинок: {img_bal}",
            )
        except Exception:
            logger.exception("image generation failed for user %s", user_id)
            update_balance(user_id, img_delta=+1)  # откат
            await safe_delete(wait_msg)
            await message.answer(
                "⚠️ Не удалось сгенерировать картинку. Попробуй позже.")
        return

    # --- Текст ---
    if not try_consume(user_id, "text"):
        await message.answer(
            "❌ Закончились текстовые запросы. "
            "Нажми «💎 Купить подписку» или «🎁 Бесплатные запросы».")
        return

    selected_model = MODEL_TEXT_SMART if mode == "smart" else MODEL_TEXT_FAST
    wait_msg = await message.answer("⏳ Думаю над ответом...")

    try:
        response = await ai_client.chat.completions.create(
            model=selected_model,
            messages=[
                {"role": "system",
                 "content": "Ты полезный ИИ-помощник. Отвечай чётко, "
                            "структурированно и на русском языке."},
                {"role": "user", "content": message.text},
            ],
            max_tokens=4000 if mode == "smart" else 2000,
        )
        answer_text = response.choices[0].message.content or "(пустой ответ)"
        await safe_delete(wait_msg)
        await safe_send_text(message, answer_text)
    except Exception:
        logger.exception("text completion failed for user %s", user_id)
        update_balance(user_id, text_delta=+1)  # откат
        await safe_delete(wait_msg)
        await message.answer(
            "⚠️ Нейросеть временно недоступна. Попробуй ещё раз.")


@dp.message()
async def fallback(message: types.Message):
    await message.answer(
        "Пожалуйста, отправь текстовый запрос или выбери режим в меню 👇",
        reply_markup=main_kb,
    )


# ---------- Запуск ----------
async def main() -> None:
    init_db()
    global bot_username
    try:
        me = await bot.get_me()
        bot_username = me.username or ""
        logger.info("Bot started as @%s", bot_username)
    except Exception:
        logger.exception("get_me failed")

    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dp.start_polling(bot)
    finally:
        await ai_client.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
