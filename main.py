import os
import base64
import asyncio
import logging
import sqlite3
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BufferedInputFile,
)
from openai import AsyncOpenAI

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_KEY = os.getenv("API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", "1995531110"))

BASE_URL = "https://api.proxyapi.ru/v1"

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
            (user_id, ref_id),
        )
        if ref_id and ref_id != user_id:
            cur.execute(
                "UPDATE users SET text_balance = text_balance + 5, img_balance = img_balance + 1 WHERE user_id = ?",
                (ref_id,),
            )
        conn.commit()
        row = (5, 1)
        is_new = True
    conn.close()
    return row, is_new


def update_balance(user_id: int, text_delta: int = 0, img_delta: int = 0):
    conn = sqlite3.connect("users.db")
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET text_balance = MAX(0, text_balance + ?), img_balance = MAX(0, img_balance + ?) WHERE user_id = ?",
        (text_delta, img_delta, user_id),
    )
    conn.commit()
    conn.close()


def get_stats():
    conn = sqlite3.connect("users.db")
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*), SUM(text_balance), SUM(img_balance) FROM users")
    res = cur.fetchone()
    conn.close()
    if not res:
        return 0, 0, 0
    total_users, total_text, total_img = res
    return total_users or 0, total_text or 0, total_img or 0


def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="⚡ Быстрый ответ"), KeyboardButton(text="🧠 Умный режим")],
            [KeyboardButton(text="🎨 Создать картинку"), KeyboardButton(text="👤 Профиль")],
            [KeyboardButton(text="💎 Купить подписку"), KeyboardButton(text="🎁 Бесплатные запросы")],
        ],
        resize_keyboard=True,
    )


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    parts = iter(message.text.split())
    next(parts, None)
    second_word = next(parts, "")
    ref_id = int(second_word) if second_word.isdigit() else 0

    (text_bal, img_bal), is_new = get_user(message.from_user.id, ref_id)
    user_modes[message.from_user.id] = "fast"

    if is_new and ref_id and ref_id != message.from_user.id:
        try:
            await bot.send_message(
                ref_id,
                "🎉 По твоей ссылке зашел друг! Тебе начислено **+5 текстов** и **+1 картинка**.",
                parse_mode="Markdown",
            )
        except Exception:
            pass

    await message.answer(
        f"👋 Привет, **{message.from_user.first_name}**!\n\n"
        f"Я твой ИИ-помощник. Выбери нужный режим в меню ниже и просто напиши свой запрос.\n\n"
        f"📊 Твой баланс:\n"
        f"• Текстовые запросы: **{text_bal}**\n"
        f"• Генерация картинок: **{img_bal}**",
        reply_markup=main_keyboard(),
        parse_mode="Markdown",
    )


@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    total_users, total_text, total_img = get_stats()
    await message.answer(
        f"🛠 **Панель администратора**\n\n"
        f"👥 Всего пользователей: **{total_users}**\n"
        f"💬 Остаток текстов у всех: **{total_text}**\n"
        f"🎨 Остаток картинок у всех: **{total_img}**\n\n"
        f"➕ Начислить баланс вручную:\n"
        f"`/add ID тексты картинки`\n"
        f"Пример: `/add {message.from_user.id} 10 5`",
        parse_mode="Markdown",
    )


@dp.message(Command("add"))
async def cmd_add(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split()
    if len(parts) != 4:
        await message.answer("⚠️ Формат: `/add ID тексты картинки`", parse_mode="Markdown")
        return
    try:
        _, target_str, add_t_str, add_i_str = parts
        target_id = int(target_str)
        add_t = int(add_t_str)
        add_i = int(add_i_str)

        get_user(target_id)
        update_balance(target_id, text_delta=add_t, img_delta=add_i)
        await message.answer(
            f"✅ Пользователю `{target_id}` начислено +{add_t} текстов и +{add_i} картинок.",
            parse_mode="Markdown",
        )
        if target_id != message.from_user.id:
            try:
                await bot.send_message(
                    target_id,
                    f"🎁 Администратор начислил тебе **+{add_t} текстов** и **+{add_i} картинок**!",
                    parse_mode="Markdown",
                )
            except Exception:
                pass
    except ValueError:
        await message.answer("⚠️ Все параметры после `/add` должны быть целыми числами.", parse_mode="Markdown")


@dp.message(F.text == "⚡ Быстрый ответ")
async def mode_fast(message: types.Message):
    user_modes[message.from_user.id] = "fast"
    await message.answer("⚡ Режим **Быстрый ответ** включен. Напиши свой вопрос:", parse_mode="Markdown")


@dp.message(F.text == "🧠 Умный режим")
async def mode_smart(message: types.Message):
    user_modes[message.from_user.id] = "smart"
    await message.answer("🧠 Режим **Умный режим** включен. Задай сложную задачу:", parse_mode="Markdown")


@dp.message(F.text == "🎨 Создать картинку")
async def mode_image(message: types.Message):
    user_modes[message.from_user.id] = "image"
    await message.answer("🎨 Режим **Генерация картинок** включен. Опиши, что нарисовать:", parse_mode="Markdown")


@dp.message(F.text == "👤 Профиль")
async def show_profile(message: types.Message):
    (text_bal, img_bal), _ = get_user(message.from_user.id)
    current_mode = user_modes.get(message.from_user.id, "fast")
    mode_names = {
        "fast": "⚡ Быстрый ответ",
        "smart": "🧠 Умный режим",
        "image": "🎨 Генерация картинок",
    }
    await message.answer(
        f"👤 **Твой профиль**\n\n"
        f"🆔 Твой ID: `{message.from_user.id}`\n"
        f"⚙️ Активный режим: **{mode_names.get(current_mode, '⚡ Быстрый ответ')}**\n\n"
        f"📊 **Баланс:**\n"
        f"• Текстовые запросы: **{text_bal}**\n"
        f"• Генерация картинок: **{img_bal}**",
        parse_mode="Markdown",
    )


@dp.message(F.text == "🎁 Бесплатные запросы")
async def show_referral(message: types.Message):
    bot_info = await bot.get_me()
    ref_link = f"https://t.me{bot_info.username}?start={message.from_user.id}"
    await message.answer(
        f"🎁 **Получи запросы бесплатно!**\n\n"
        f"Приглашай друзей по своей ссылке. За каждого нового пользователя ты получишь **+5 текстов** и **+1 картинку**.\n\n"
        f"🔗 Твоя ссылка:\n`{ref_link}`",
        parse_mode="Markdown",
    )


@dp.message(F.text == "💎 Купить подписку")
async def show_tariffs(message: types.Message):
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⭐ Старт — 50 текстов + 5 картинок (199 ₽)", callback_data="buy_start")],
            [InlineKeyboardButton(text="🚀 Про — 200 текстов + 25 картинок (499 ₽)", callback_data="buy_pro")],
        ]
    )
    await message.answer(
        "💎 **Пополнение баланса**\n\nВыбери подходящий пакет запросов:",
        reply_markup=kb,
        parse_mode="Markdown",
    )


@dp.callback_query(F.data.startswith("buy_"))
async def process_buy(callback: types.CallbackQuery):
    await callback.message.answer(
        f"💳 Для оформления тарифа напиши администратору и укажи свой ID: `{callback.from_user.id}`",
        parse_mode="Markdown",
    )
    await callback.answer()


@dp.message(F.text)
async def handle_ai_request(message: types.Message):
    user_id = message.from_user.id
    (text_bal, img_bal), _ = get_user(user_id)
    mode = user_modes.get(user_id, "fast")

    if mode == "image":
        if img_bal <= 0:
            await message.answer(
                "❌ Закончились генерации картинок. Нажми «💎 Купить подписку» или «🎁 Бесплатные запросы»."
            )
            return

        wait_msg = await message.answer("🎨 Рисую картинку, подождите 10–15 секунд...")
        try:
            response = await ai_client.images.generate(
                model=MODEL_IMAGE,
                prompt=message.text,
                n=1,
                size="1024x1024",
            )
            item = next(iter(response.data))
            if getattr(item, "url", None):
                photo_to_send = item.url
            elif getattr(item, "b64_json", None):
                img_bytes = base64.b64decode(item.b64_json)
                photo_to_send = BufferedInputFile(img_bytes, filename="image.png")
            else:
                raise ValueError("API не вернул изображение")

            update_balance(user_id, img_delta=-1)
            await message.answer_photo(
