import os
import base64
import asyncio
import logging
import sqlite3
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from openai import AsyncOpenAI

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_KEY = os.getenv("API_KEY")
ADMIN_ID = 1995531110

BASE_URL = "https://api." + "proxyapi.ru/v1"
MODEL_TEXT_FAST = "openai/gpt-6.2-sol"
MODEL_TEXT_SMART = "anthropic/claude-sonnet-5.5"
MODEL_IMAGE = "openai/gpt-image-2.6-flare"

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
ai_client = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL)

user_modes = {}

def init_db():
    conn = sqlite3.connect("users.db")
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            text_balance INTEGER DEFAULT 5,
            img_balance INTEGER DEFAULT 1
        )
    """)
    conn.commit()
    conn.close()

def get_user(user_id: int):
    conn = sqlite3.connect("users.db")
    cur = conn.cursor()
    cur.execute("SELECT text_balance, img_balance FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    if not row:
        cur.execute("INSERT INTO users (user_id, text_balance, img_balance) VALUES (?, 5, 1)", (user_id,))
        conn.commit()
        row = (5, 1)
    conn.close()
    return row

def update_balance(user_id: int, text_delta: int = 0, img_delta: int = 0):
    if user_id == ADMIN_ID and text_delta < 0 and img_delta <= 0:
        return
    if user_id == ADMIN_ID and img_delta < 0 and text_delta <= 0:
        return
    conn = sqlite3.connect("users.db")
    cur = conn.cursor()
    cur.execute("""
        UPDATE users 
        SET text_balance = text_balance + ?, img_balance = img_balance + ? 
        WHERE user_id = ?
    """, (text_delta, img_delta, user_id))
    conn.commit()
    conn.close()

def get_stats():
    conn = sqlite3.connect("users.db")
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*), SUM(text_balance), SUM(img_balance) FROM users")
    row = cur.fetchone()
    conn.close()
    return row

main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="✍️ Написать / Решить задачу"), KeyboardButton(text="🎨 Создать картинку")],
        [KeyboardButton(text="🧠 Сложный анализ / Код"), KeyboardButton(text="👤 Мой профиль / Баланс")],
        [KeyboardButton(text="💎 Купить подписку")]
    ],
    resize_keyboard=True
)

tariffs_kb = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Старт: 100 текстов + 10 картинок (290 ₽)", callback_data="buy_start")],
        [InlineKeyboardButton(text="Оптимальный: 300 текстов + 35 картинок (590 ₽)", callback_data="buy_opt")],
        [InlineKeyboardButton(text="Профи: 1000 текстов + 80 картинок (1190 ₽)", callback_data="buy_pro")]
    ]
)

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    text_bal, img_bal = get_user(message.from_user.id)
    user_modes[message.from_user.id] = "fast"
    bal_info = "♾️ **Безлимит (Администратор)**" if message.from_user.id == ADMIN_ID else f"**{text_bal} текстовых запросов** и **{img_bal} генерация картинки**"
    await message.answer(
        "👋 **Привет! Я твой универсальный ИИ-помощник.**\n\n"
        "Здесь собраны лучшие нейросети мира — **без зарубежных карт и сложных настроек**.\n\n"
        f"🎁 Твой баланс: {bal_info}.\n\n"
        "👇 Выбери режим в меню ниже и просто напиши свой запрос:",
        reply_markup=main_kb,
        parse_mode="Markdown"
    )

@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    total_users, total_texts, total_imgs = get_stats()
    await message.answer(
        "👑 **Панель администратора**\n\n"
        f"👥 Всего пользователей в боте: **{total_users or 0}**\n"
        f"💬 Общий остаток текстов у юзеров: **{total_texts or 0}**\n"
        f"🎨 Общий остаток картинок у юзеров: **{total_imgs or 0}**\n\n"
        "➕ **Начислить пакет клиенту:**\n"
        "`/add ID_клиента тексты картинки`\n"
        "Пример: `/add 123456789 100 10`",
        parse_mode="Markdown"
    )

@dp.message(Command("add"))
async def cmd_add_balance(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split()
    if len(parts) != 4:
        await message.answer("⚠️ Формат: `/add ID тексты картинки`\nПример: `/add 123456789 100 10`", parse_mode="Markdown")
        return
    try:
        target_id = int(parts[1])
        add_text = int(parts[2])
        add_img = int(parts[3])
        get_user(target_id)
        update_balance(target_id, text_delta=add_text, img_delta=add_img)
        await message.answer(f"✅ Пользователю `{target_id}` начислено: **+{add_text} текстов** и **+{add_img} картинок**!", parse_mode="Markdown")
        try:
            await bot.send_message(target_id, f"🎉 **Оплата подтверждена!**\nНа ваш баланс зачислено: **+{add_text} текстовых запросов** и **+{add_img} картинок**.", parse_mode="Markdown")
        except Exception:
            pass
    except Exception as e:
        await message.answer(f"⚠️ Ошибка команды: {e}")

@dp.message(F.text == "✍️ Написать / Решить задачу")
async def mode_fast(message: types.Message):
    user_modes[message.from_user.id] = "fast"
    await message.answer("✍️ Режим **Быстрый текст / Учеба** включен. Напиши свой вопрос:", parse_mode="Markdown")

@dp.message(F.text == "🧠 Сложный анализ / Код")
async def mode_smart(message: types.Message):
    user_modes[message.from_user.id] = "smart"
    await message.answer("🧠 Режим **Глубокий анализ / Код (Claude)** включен. Опиши задачу:", parse_mode="Markdown")

@dp.message(F.text == "🎨 Создать картинку")
async def mode_image(message: types.Message):
    user_modes[message.from_user.id] = "image"
    await message.answer("🎨 Режим **Генерация картинок** включен. Опиши, что нарисовать:", parse_mode="Markdown")

@dp.message(F.text == "👤 Мой профиль / Баланс")
async def show_profile(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await message.answer(
            f"👑 **Профиль Администратора (ID: `{message.from_user.id}`)**\n\n"
            "💬 Текстовые запросы: **♾️ Безлимит**\n"
            "🎨 Генерация картинок: **♾️ Безлимит**\n\n"
            "Команда статистики: /admin",
            parse_mode="Markdown"
        )
        return
    text_bal, img_bal = get_user(message.from_user.id)
    await message.answer(
        f"👤 **Твой профиль (ID: `{message.from_user.id}`)**\n\n"
        f"💬 Остаток текстовых запросов: **{text_bal}**\n"
        f"🎨 Остаток картинок: **{img_bal}**",
        parse_mode="Markdown"
    )

@dp.message(F.text == "💎 Купить подписку")
async def show_tariffs(message: types.Message):
    await message.answer(
        "💎 **Выбери подходящий пакет запросов:**\n\n"
        "Оплата по СБП и картами РФ.",
        reply_markup=tariffs_kb,
        parse_mode="Markdown"
    )

@dp.callback_query(F.data.startswith("buy_"))
async def process_buy(callback: types.CallbackQuery):
    tariffs = {
        "buy_start": ("Старт (100 текстов + 10 картинок)", "290 ₽"),
        "buy_opt": ("Оптимальный (300 текстов + 35 картинок)", "590 ₽"),
        "buy_pro": ("Профи (1000 текстов + 80 картинок)", "1190 ₽")
    }
    name, price = tariffs.get(callback.data, ("Пакет", "290 ₽"))
    await callback.message.answer(
        f"💳 **Оформление тарифа «{name}» — {price}**\n\n"
        f"Ваш ID для зачисления: `{callback.from_user.id}`\n\n"
        "Автоматическая оплата по СБП сейчас подключается. Чтобы активировать пакет прямо сейчас, перешлите это сообщение администратору.",
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.message(F.text)
async def handle_ai_request(message: types.Message):
    user_id = message.from_user.id
    is_admin = (user_id == ADMIN_ID)
    text_bal, img_bal = get_user(user_id)
    mode = user_modes.get(user_id, "fast")

    if mode == "image":
        if not is_admin and img_bal <= 0:
            await message.answer("❌ Закончились генерации картинок. Нажми «💎 Купить подписку».")
            return

        wait_msg = await message.answer("🎨 Рисую картинку, подождите 15–20 секунд...")
        try:
            response = await ai_client.images.generate(
                model=MODEL_IMAGE,
                prompt=message.text,
                n=1,
                size="1024x1024"
            )
            item = next(iter(response.data))
            if getattr(item, "b64_json", None):
                img_bytes = base64.b64decode(item.b64_json)
                photo_file = BufferedInputFile(img_bytes, filename="image.png")
            else:
                photo_file = item.url

            if not is_admin:
                update_balance(user_id, img_delta=-1)
            await wait_msg.delete()
            cap = "✅ Готово! (Режим Админа)" if is_admin else f"✅ Готово! Осталось картинок: {img_bal - 1}"
            await message.answer_photo(photo=photo_file, caption=cap)
        except Exception as e:
            await wait_msg.edit_text(f"⚠️ Ошибка генерации: {e}")
        return

    if not is_admin and text_bal <= 0:
        await message.answer("❌ Закончились текстовые запросы. Нажми «💎 Купить подписку».")
        return

    wait_msg = await message.answer("⏳ Думаю над ответом...")
    selected_model = MODEL_TEXT_SMART if mode == "smart" else MODEL_TEXT_FAST

    try:
        response = await ai_client.chat.completions.create(
            model=selected_model,
            messages=[
                {"role": "system", "content": "Ты полезный ИИ-помощник. Отвечай четко, структурированно и на русском языке."},
                {"role": "user", "content": message.text}
            ],
            max_tokens=2000
        )
