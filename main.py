import os
import asyncio
import logging
import sqlite3
import html
import base64
import io
from contextlib import closing
from datetime import datetime

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
MODEL_VOICE = "whisper-1"

DB_PATH = "users.db"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ai-bot")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
ai_client = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL)

user_modes = {}
bot_username = None

# ---------- БД ----------
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            text_balance INTEGER DEFAULT 5,
            img_balance INTEGER DEFAULT 1,
            referred_by INTEGER DEFAULT 0,
            has_made_request INTEGER DEFAULT 0,
            ref_bonus_paid INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    return conn

def get_user(user_id, ref_id=0):
    with closing(get_conn()) as conn:
        cur = conn.execute("SELECT text_balance, img_balance FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        if row:
            return (row[0], row[1]), "existing"
        
        valid_ref = 0
        if ref_id and ref_id != user_id:
            cur = conn.execute("SELECT 1 FROM users WHERE user_id = ?", (ref_id,))
            if cur.fetchone():
                valid_ref = ref_id

        conn.execute("INSERT INTO users (user_id, text_balance, img_balance, referred_by) VALUES (?, 5, 1, ?)", (user_id, valid_ref))
        conn.commit()
        return (5, 1), "new"

def update_balance(user_id, text_delta=0, img_delta=0):
    with closing(get_conn()) as conn:
        conn.execute("UPDATE users SET text_balance = text_balance + ?, img_balance = img_balance + ? WHERE user_id = ?", (text_delta, img_delta, user_id))
        conn.commit()

def try_consume(user_id, kind, amount=1):
    col = "text_balance" if kind == "text" else "img_balance"
    with closing(get_conn()) as conn:
        cur = conn.execute(f"UPDATE users SET {col} = {col} - ? WHERE user_id = ? AND {col} >= ?", (amount, user_id, amount))
        conn.commit()
        return cur.rowcount > 0

# ---------- Клавиатуры ----------
main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="✍️ Написать / Решить задачу"), KeyboardButton(text="🎨 Создать картинку")],
        [KeyboardButton(text="🧠 Сложный анализ / Код"), KeyboardButton(text="👤 Мой профиль / Баланс")],
        [KeyboardButton(text="🎁 Бесплатные запросы"), KeyboardButton(text="💎 Купить подписку")],
    ], resize_keyboard=True,
)

buy_menu_kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="📦 Купить пакет (выгодно)", callback_data="menu_packs")],
    [InlineKeyboardButton(text="🛒 Купить поштучно", callback_data="menu_single")]
])

packs_kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="Старт (200 ⭐)", callback_data="buy_pack_start")],
    [InlineKeyboardButton(text="Оптимальный (550 ⭐)", callback_data="buy_pack_opt")],
    [InlineKeyboardButton(text="Профи (1500 ⭐)", callback_data="buy_pack_pro")],
    [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_back")]
])

single_kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="1 Генерация картинки (15 ⭐)", callback_data="buy_single_img")],
    [InlineKeyboardButton(text="10 Текстовых запросов (10 ⭐)", callback_data="buy_single_text")],
    [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_back")]
])

# ---------- Хендлеры ----------
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    args = message.text.split(maxsplit=1)
    ref_id = int(args[1]) if len(args) > 1 and args[1].isdigit() else 0

    (text_bal, img_bal), status = get_user(message.from_user.id, ref_id)
    user_modes[message.from_user.id] = "fast"

    # ПРИНУДИТЕЛЬНЫЙ БОНУС ПРИ КАЖДОМ СТАРТЕ
    bonus_text = "\n\n🎁 *Тестовый бонус:* +1 запрос!"
    update_balance(message.from_user.id, text_delta=1)
    new_balance = text_bal + 1

    await message.answer(
        f"👋 *Привет!* Твой баланс: *{new_balance}* текстовых и *{img_bal}* картинок."
        f"{bonus_text}\n\n"
        "👇 Выбери режим в меню ниже и просто напиши свой запрос:",
        reply_markup=main_kb, parse_mode="Markdown",
    )

@dp.message(F.text == "👤 Мой профиль / Баланс")
async def show_profile(message: types.Message):
    (text_bal, img_bal), status = get_user(message.from_user.id)
    await message.answer(f"👤 *Профиль*\n\n💬 Текстовых запросов: *{text_bal}*\n🎨 Картинок: *{img_bal}*", parse_mode="Markdown")

@dp.message(F.text == "✍️ Написать / Решить задачу")
async def mode_fast(message: types.Message):
    user_modes[message.from_user.id] = "fast"
    await message.answer("✍️ Режим *Быстрый текст* включён.", parse_mode="Markdown")

@dp.message(F.text == "🧠 Сложный анализ / Код")
async def mode_smart(message: types.Message):
    user_modes[message.from_user.id] = "smart"
    await message.answer("🧠 Режим *Глубокий анализ* включён.", parse_mode="Markdown")

@dp.message(F.text == "🎨 Создать картинку")
async def mode_image(message: types.Message):
    user_modes[message.from_user.id] = "image"
    await message.answer("🎨 Режим *Генерация картинок* включён.", parse_mode="Markdown")

@dp.message(F.text == "🎁 Бесплатные запросы")
async def show_referral(message: types.Message):
    global bot_username
    if bot_username is None:
        me = await bot.get_me()
        bot_username = me.username or ""
    ref_link = f"https://t.me/{bot_username}?start={message.from_user.id}"
    await message.answer(f"🎁 *Твоя ссылка:*\n`{ref_link}`", parse_mode="Markdown")

@dp.message(F.text == "💎 Купить подписку")
async def show_tariffs(message: types.Message):
    await message.answer("💎 *Выбери способ пополнения:*", reply_markup=buy_menu_kb, parse_mode="Markdown")

@dp.callback_query(F.data == "menu_packs")
async def cb_menu_packs(callback: types.CallbackQuery):
    await callback.message.edit_text("📦 *Выбери пакет:*", reply_markup=packs_kb, parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "menu_single")
async def cb_menu_single(callback: types.CallbackQuery):
    await callback.message.edit_text("🛒 *Купить поштучно:*", reply_markup=single_kb, parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "menu_back")
async def cb_menu_back(callback: types.CallbackQuery):
    await callback.message.edit_text("💎 *Выбери способ пополнения:*", reply_markup=buy_menu_kb, parse_mode="Markdown")
    await callback.answer()

# ---------- ОПЛАТА ----------
async def send_invoice(chat_id, title, description, payload, stars):
    await bot.send_invoice(chat_id=chat_id, title=title, description=description, payload=payload, provider_token="", currency="XTR", prices=[LabeledPrice(label=title, amount=stars)])

@dp.callback_query(F.data.startswith("buy_"))
async def cb_buy(callback: types.CallbackQuery):
    data = callback.data
    if data == "buy_pack_start": await send_invoice(callback.message.chat.id, "Старт", "100 текстов + 10 картинок", data, 200)
    elif data == "buy_pack_opt": await send_invoice(callback.message.chat.id, "Оптимальный", "300 текстов + 35 картинок", data, 550)
    elif data == "buy_pack_pro": await send_invoice(callback.message.chat.id, "Профи", "1000 текстов + 80 картинок", data, 1500)
    elif data == "buy_single_img": await send_invoice(callback.message.chat.id, "1 Картинка", "Одна генерация", data, 15)
    elif data == "buy_single_text": await send_invoice(callback.message.chat.id, "10 Текстов", "Пакет из 10", data, 10)
    await callback.answer()

@dp.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@dp.message(F.successful_payment)
async def process_successful_payment(message: types.Message):
    payload = message.successful_payment.invoice_payload
    user_id = message.from_user.id
    if payload == "buy_pack_start": update_balance(user_id, 100, 10); await message.answer("✅ Начислено 100 текстов и 10 картинок!")
    elif payload == "buy_pack_opt": update_balance(user_id, 300, 35); await message.answer("✅ Начислено 300 текстов и 35 картинок!")
    elif payload == "buy_pack_pro": update_balance(user_id, 1000, 80); await message.answer("✅ Начислено 1000 текстов и 80 картинок!")
    elif payload == "buy_single_img": update_balance(user_id, 0, 1); await message.answer("✅ Начислена 1 картинка!")
    elif payload == "buy_single_text": update_balance(user_id, 10, 0); await message.answer("✅ Начислено 10 текстов!")

# ---------- ОБРАБОТКА ----------
@dp.message(F.text & ~F.text.startswith("/"))
async def handle_text(message: types.Message):
    user_id = message.from_user.id
    mode = user_modes.get(user_id, "fast")

    if mode == "image":
        if not try_consume(user_id, "img"): await message.answer("❌ Закончились генерации."); return
        wait_msg = await message.answer("🎨 Рисую...")
        try:
            response = await ai_client.images.generate(model=MODEL_IMAGE, prompt=message.text)
            image_data = response.data[0]
            url = image_data.url if image_data.url else f"data:image/png;base64,{image_data.b64_json}"
            await wait_msg.delete()
            (_, img_bal), _ = get_user(user_id)
            await message.answer_photo(photo=url, caption=f"✅ Готово! Осталось: {img_bal}")
        except Exception as e:
            update_balance(user_id, img_delta=1)
            await wait_msg.delete()
            await message.answer(f"⚠️ Ошибка: {str(e)[:100]}")
        return

    cost = 5 if mode == "smart" else 1
    if not try_consume(user_id, "text", amount=cost):
        await message.answer(f"❌ Недостаточно запросов (нужно {cost}).")
        return

    selected_model = MODEL_TEXT_SMART if mode == "smart" else MODEL_TEXT_FAST
    wait_msg = await message.answer("⏳ Думаю...")
    try:
        response = await ai_client.chat.completions.create(model=selected_model, messages=[{"role": "user", "content": message.text}], max_tokens=2000)
        await wait_msg.delete()
        await message.answer(response.choices[0].message.content)
    except Exception as e:
        update_balance(user_id, text_delta=cost)
        await wait_msg.delete()
        await message.answer(f"⚠️ Ошибка: {str(e)[:100]}")

@dp.message(F.voice)
async def handle_voice(message: types.Message):
    user_id = message.from_user.id
    if not try_consume(user_id, "text", amount=2):
        await message.answer("❌ Закончились запросы (для голоса нужно 2).")
        return

    wait_msg = await message.answer("🎤 Распознаю...")
    try:
        file_info = await bot.get_file(message.voice.file_id)
        file_bytes = await bot.download_file(file_info.file_path)
        transcript = await ai_client.audio.transcriptions.create(model=MODEL_VOICE, file=("voice.ogg", file_bytes.read()))
        text_query = transcript.text
        if not text_query: raise ValueError("Не распознано")
        await wait_msg.edit_text(f"🗣️ *Вы сказали:* {text_query}\n\n⏳ Думаю...", parse_mode="Markdown")
        response = await ai_client.chat.completions.create(model=MODEL_TEXT_FAST, messages=[{"role": "user", "content": text_query}], max_tokens=2000)
        await wait_msg.delete()
        await message.answer(response.choices[0].message.content)
    except Exception as e:
        update_balance(user_id, text_delta=2)
        await wait_msg.delete()
        await message.answer(f"⚠️ Ошибка: {str(e)[:100]}")

@dp.message()
async def fallback(message: types.Message):
    await message.answer("Отправь текст, голос или фото, либо выбери режим в меню 👇", reply_markup=main_kb)

# ---------- Запуск ----------
async def main():
    with closing(get_conn()) as conn: pass
    global bot_username
    try:
        me = await bot.get_me()
        bot_username = me.username or ""
    except: pass

    await bot.delete_webhook(drop_pending_updates=True)
    try: await dp.start_polling(bot)
    finally:
        await ai_client.close()
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
