import os
import asyncio
import logging
import sqlite3
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from openai import AsyncOpenAI

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_KEY = os.getenv("API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

BASE_URL = "https://proxyapi.ru"
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
            img_balance INTEGER DEFAULT 1,
            referred_by INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

def get_user(user_id: int, ref_id: int = 0):
    conn = sqlite3.connect("users.db")
    cur = conn.cursor()
    cur.execute("SELECT text_balance, img_balance FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    is_new = False
    if not row:
        cur.execute(
            "INSERT INTO users (user_id, text_balance, img_balance, referred_by) VALUES (?, 5, 1, ?)",
            (user_id, ref_id)
        )
        if ref_id and ref_id != user_id:
            cur.execute(
                "UPDATE users SET text_balance = text_balance + 5, img_balance = img_balance + 1 WHERE user_id = ?",
                (ref_id,)
            )
        conn.commit()
        row = (5, 1)
        is_new = True
    conn.close()
    return row, is_new

def update_balance(user_id: int, text_delta: int = 0, img_delta: int = 0):
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
    stats = cur.fetchone()
    conn.close()
    return stats

main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="✍️ Написать / Решить задачу"), KeyboardButton(text="🎨 Создать картинку")],
        [KeyboardButton(text="🧠 Сложный анализ / Код"), KeyboardButton(text="👤 Мой профиль / Баланс")],
        [KeyboardButton(text="🎁 Бесплатные запросы"), KeyboardButton(text="💎 Купить подписку")]
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
    args = message.text.split()
    ref_id = int(args) if len(args) > 1 and args.isdigit() else 0
    (text_bal, img_bal), is_new = get_user(message.from_user.id, ref_id)
    user_modes[message.from_user.id] = "fast"

    if is_new and ref_id and ref_id != message.from_user.id:
        try:
            await bot.send_message(ref_id, "🎉 По твоей ссылке зашел друг! Тебе начислено **+5 текстов** и **+1 картинка**.", parse_mode="Markdown")
        except Exception:
            pass

    await message.answer(
        "👋 **Привет! Я твой универсальный ИИ-помощник.**\n\n"
        "Здесь собраны лучшие нейросети мира — **без зарубежных карт и сложных настроек**.\n\n"
        f"🎁 Твой баланс: **{text_bal} текстовых запросов** и **{img_bal} генераций картинок**.\n\n"
        "👇 Выбери режим в меню ниже и просто напиши свой запрос:",
        reply_markup=main_kb,
        parse_mode="Markdown"
    )

@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if ADMIN_ID != 0 and message.from_user.id != ADMIN_ID:
        await message.answer(f"⛔ Доступ закрыт. Твой ID: `{message.from_user.id}`", parse_mode="Markdown")
        return
    total_users, total_texts, total_imgs = get_stats()
    await message.answer(
        "👑 **Панель администратора**\n\n"
        f"👥 Всего пользователей: **{total_users or 0}**\n"
        f"💬 Остаток текстов у юзеров: **{total_texts or 0}**\n"
        f"🎨 Остаток картинок у юзеров: **{total_imgs or 0}**\n\n"
        "➕ Начислить баланс вручную:\n"
        "`/add ID тексты картинки`\n"
        "Пример: `/add 123456789 100 10`",
        parse_mode="Markdown"
    )

@dp.message(Command("add"))
async def cmd_add_balance(message: types.Message):
    if ADMIN_ID != 0 and message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split()
    if len(parts) != 4:
        await message.answer("⚠️ Формат: `/add ID тексты картинки`", parse_mode="Markdown")
        return
    try:
        target_id = int(parts)
        add_t = int(parts)
        add_i = int(parts)
        get_user(target_id)
        update_balance(target_id, text_delta=add_t, img_delta=add_i)
        await message.answer(f"✅ Пользователю `{target_id}` начислено +{add_t} текстов и +{add_i} картинок.", parse_mode="Markdown")
        await bot.send_message(target_id, f"🎁 Администратор начислил тебе **+{add_t} текстов** и **+{add_i} картинок**!", parse_mode="Markdown")
    except Exception as e:
        await message.answer(f"⚠️ Ошибка: {e}")

@dp.message(F.text == "✍️ Написать / Решить задачу")
async def mode_fast(message: types.Message):
    user_modes[message.from_user.id] = "fast"
    await message.answer("✍️ Режим **Быстрый текст / Учеба** включен. Напиши свой вопрос:")

@dp.message(F.text == "🧠 Сложный анализ / Код")
async def mode_smart(message: types.Message):
    user_modes[message.from_user.id] = "smart"
    await message.answer("🧠 Режим **Глубокий анализ / Код (Claude)** включен. Опиши задачу:")

@dp.message(F.text == "🎨 Создать картинку")
async def mode_image(message: types.Message):
    user_modes[message.from_user.id] = "image"
    await message.answer("🎨 Режим **Генерация картинок** включен. Опиши, что нарисовать:")

@dp.message(F.text == "👤 Мой профиль / Баланс")
async def show_profile(message: types.Message):
    (text_bal, img_bal), _ = get_user(message.from_user.id)
    await message.answer(
        f"👤 **Твой профиль (ID: `{message.from_user.id}`)**\n\n"
        f"💬 Остаток текстовых запросов: **{text_bal}**\n"
        f"🎨 Остаток картинок: **{img_bal}**",
        parse_mode="Markdown"
    )

@dp.message(F.text == "🎁 Бесплатные запросы")
async def show_referral(message: types.Message):
    bot_info = await bot.get_me()
    ref_link = f"https://t.me{bot_info.username}?start={message.from_user.id}"
    await message.answer(
        "🎁 **Получай запросы бесплатно!**\n\n"
        "Отправь свою личную ссылку друзьям. За каждого нового пользователя ты получишь **+5 текстов** и **+1 картинку**:\n\n"
        f"`{ref_link}`",
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
    await callback.message.answer(
        f"💳 Для моментального пополнения напишите администратору и укажите ваш ID: `{callback.from_user.id}`",
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.message(F.text)
async def handle_ai_request(message: types.Message):
    user_id = message.from_user.id
    (text_bal, img_bal), _ = get_user(user_id)
    mode = user_modes.get(user_id, "fast")

    if mode == "image":
        if img_bal <= 0:
            await message.answer("❌ Закончились генерации картинок. Нажми «💎 Купить подписку» или «🎁 Бесплатные запросы».")
            return

        wait_msg = await message.answer("🎨 Рисую картинку, подождите 10–15 секунд...")
        try:
            response = await ai_client.images.generate(
                model=MODEL_IMAGE,
                prompt=message.text,
                n=1,
                size="1024x1024"
            )
            image_url = response.data.url
            update_balance(user_id, img_delta=-1)
            await wait_msg.delete()
            await message.answer_photo(photo=image_url, caption=f"✅ Готово! Осталось картинок: {img_bal - 1}")
        except Exception as e:
            await wait_msg.edit_text(f"⚠️ Ошибка генерации: {e}")
        return

    if text_bal <= 0:
        await message.answer("❌ Закончились текстовые запросы. Нажми «💎 Купить подписку» или «🎁 Бесплатные запросы».")
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
        answer_text = response.choices.message.content
        update_balance(user_id, text_delta=-1)
        await wait_msg.delete()
        await message.answer(answer_text)
    except Exception as e:
        await wait_msg.edit_text(f"⚠️ Ошибка нейросети: {e}")

async def main():
    init_db()

                
