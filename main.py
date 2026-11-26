import os
import asyncio
import logging
import sqlite3
import html
import base64
import io
from contextlib import closing

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
    BufferedInputFile,
    LabeledPrice, PreCheckoutQuery
)
from aiogram.exceptions import TelegramBadRequest
from openai import AsyncOpenAI

# ---------- Конфигурация ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_KEY = os.getenv("API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

BASE_URL = "https://api.proxyapi.ru/v1"

MODEL_TEXT_FAST = "openai/gpt-6.2-sol"
MODEL_TEXT_SMART = "openai/gpt-4o" 
MODEL_IMAGE = "black-forest-labs/flux.2-klein-4b" 

DB_PATH = "users.db"
TG_TEXT_LIMIT = 4000

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
    with closing(get_conn()) as conn:
        cur = conn.execute(
            "SELECT text_balance, img_balance FROM users WHERE user_id = ?",
            (user_id,),
        )
        row = cur.fetchone()
        if row:
            return (row[0], row[1]), False

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

def try_consume(user_id: int, kind: str, amount: int = 1) -> bool:
    col = "text_balance" if kind == "text" else "img_balance"
    with closing(get_conn()) as conn:
        cur = conn.execute(
            f"UPDATE users SET {col} = {col} - ? "
            f"WHERE user_id = ? AND {col} >= ?",
            (amount, user_id, amount),
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

buy_menu_kb = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="📦 Купить пакет (выгодно)", callback_data="menu_packs")],
        [InlineKeyboardButton(text="🛒 Купить поштучно", callback_data="menu_single")]
    ]
)

# ИСПРАВЛЕНО: Короткие названия, чтобы текст не обрезался
packs_kb = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Старт (200 ⭐)", callback_data="buy_pack_start")],
        [InlineKeyboardButton(text="Оптимальный (550 ⭐)", callback_data="buy_pack_opt")],
        [InlineKeyboardButton(text="Профи (1500 ⭐)", callback_data="buy_pack_pro")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_back")]
    ]
)

single_kb = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="1 Генерация картинки (15 ⭐)", callback_data="buy_single_img")],
        [InlineKeyboardButton(text="10 Текстовых запросов (10 ⭐)", callback_data="buy_single_text")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_back")]
    ]
)

# ---------- Утилиты ----------
def split_text(text: str, limit: int = TG_TEXT_LIMIT) -> list[str]:
    return [text[i:i + limit] for i in range(0, len(text), limit)] or [""]

async def safe_send_text(message: types.Message, text: str) -> None:
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

    get_user(target_id)
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
    await message.answer("🧠 Режим *Глубокий анализ / Код (GPT-4o)* включён.\n"
                         "⚠️ Внимание: этот режим списывает *5 запросов* за один ответ.",
                         parse_mode="Markdown")

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
        "💎 *Выбери способ пополнения:*\n\n"
        "Ты можешь купить выгодный пакет запросов или приобрести их поштучно.",
        reply_markup=buy_menu_kb,
        parse_mode="Markdown",
    )

@dp.callback_query(F.data == "menu_packs")
async def cb_menu_packs(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "📦 *Выбери подходящий пакет запросов:*\n\n"
        "Оплата происходит в Telegram Stars (⭐).",
        reply_markup=packs_kb,
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == "menu_single")
async def cb_menu_single(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "🛒 *Купить поштучно:*\n\n"
        "Если нужно срочно сгенерировать картинку или написать пару текстов.",
        reply_markup=single_kb,
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == "menu_back")
async def cb_menu_back(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "💎 *Выбери способ пополнения:*\n\n"
        "Ты можешь купить выгодный пакет запросов или приобрести их поштучно.",
        reply_markup=buy_menu_kb,
        parse_mode="Markdown"
    )
    await callback.answer()

# ---------- ОПЛАТА ЗВЕЗДАМИ (STARS) ----------

async def send_invoice(chat_id: int, title: str, description: str, payload: str, stars_amount: int):
    await bot.send_invoice(
        chat_id=chat_id,
        title=title,
        description=description,
        payload=payload,
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=title, amount=stars_amount)],
    )

@dp.callback_query(F.data == "buy_pack_start")
async def cb_buy_pack_start(callback: types.CallbackQuery):
    await send_invoice(callback.message.chat.id, "Тариф Старт", "100 текстов + 10 картинок", "pack_start", 200)
    await callback.answer()

@dp.callback_query(F.data == "buy_pack_opt")
async def cb_buy_pack_opt(callback: types.CallbackQuery):
    await send_invoice(callback.message.chat.id, "Тариф Оптимальный", "300 текстов + 35 картинок", "pack_opt", 550)
    await callback.answer()

@dp.callback_query(F.data == "buy_pack_pro")
async def cb_buy_pack_pro(callback: types.CallbackQuery):
    await send_invoice(callback.message.chat.id, "Тариф Профи", "1000 текстов + 80 картинок", "pack_pro", 1500)
    await callback.answer()

@dp.callback_query(F.data == "buy_single_img")
async def cb_buy_single_img(callback: types.CallbackQuery):
    await send_invoice(callback.message.chat.id, "1 Генерация картинки", "Одна генерация нейросети", "single_img", 15)
    await callback.answer()

@dp.callback_query(F.data == "buy_single_text")
async def cb_buy_single_text(callback: types.CallbackQuery):
    await send_invoice(callback.message.chat.id, "10 Текстовых запросов", "Пакет из 10 текстовых запросов", "single_text", 10)
    await callback.answer()

@dp.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@dp.message(F.successful_payment)
async def process_successful_payment(message: types.Message):
    payload = message.successful_payment.invoice_payload
    user_id = message.from_user.id

    if payload == "pack_start":
        update_balance(user_id, text_delta=100, img_delta=10)
        await message.answer("✅ Оплата прошла! Тебе начислено *100 текстов* и *10 картинок*.")
    elif payload == "pack_opt":
        update_balance(user_id, text_delta=300, img_delta=35)
        await message.answer("✅ Оплата прошла! Тебе начислено *300 текстов* и *35 картинок*.")
    elif payload == "pack_pro":
        update_balance(user_id, text_delta=1000, img_delta=80)
        await message.answer("✅ Оплата прошла! Тебе начислено *1000 текстов* и *80 картинок*.")
    elif payload == "single_img":
        update_balance(user_id, img_delta=1)
        await message.answer("✅ Оплата прошла! Тебе начислена *1 генерация картинки*.")
    elif payload == "single_text":
        update_balance(user_id, text_delta=10)
        await message.answer("✅ Оплата прошла! Тебе начислено *10 текстовых запросов*.")


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
            )
            if response.data and len(response.data) > 0:
                image_data = response.data[0]
                
                if image_data.url:
                    await safe_delete(wait_msg)
                    (_, img_bal), _ = get_user(user_id)
                    await message.answer_photo(
                        photo=image_data.url,
                        caption=f"✅ Готово! Осталось картинок: {img_bal}",
                    )
                elif image_data.b64_json:
                    photo_bytes = io.BytesIO(base64.b64decode(image_data.b64_json))
                    await safe_delete(wait_msg)
                    (_, img_bal), _ = get_user(user_id)
                    await message.answer_photo(
                        photo=BufferedInputFile(photo_bytes.read(), filename="image.png"),
                        caption=f"✅ Готово! Осталось картинок: {img_bal}",
                    )
                else:
                    raise ValueError("Пустой ответ от API")
            else:
                raise ValueError("API вернуло пустой список данных")
                
        except Exception as e:
            logger.error(f"Ошибка картинки: {e}")
            update_balance(user_id, img_delta=+1)
            await safe_delete(wait_msg)
            await message.answer(f"⚠️ Ошибка API: {str(e)[:200]}")
        return

    # --- Текст ---
    cost = 5 if mode == "smart" else 1
    
    if not try_consume(user_id, "text", amount=cost):
        await message.answer(
            f"❌ Недостаточно текстовых запросов (нужно {cost}). "
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
            max_tokens=2000,
        )
        answer_text = response.choices[0].message.content or "(пустой ответ)"
        await safe_delete(wait_msg)
        await safe_send_text(message, answer_text)
    except Exception as e:
        logger.exception("text completion failed for user %s", user_id)
        update_balance(user_id, text_delta=cost)
        await safe_delete(wait_msg)
        await message.answer(f"⚠️ Ошибка текста: {str(e)[:200]}")

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
