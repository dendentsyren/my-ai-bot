import os
import asyncio
import logging
import sqlite3
import html
import base64
import io
from contextlib import closing
from datetime import datetime, timedelta

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("ai-bot")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
ai_client = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL)

user_modes: dict[int, str] = {}
bot_username: str | None = None

# Временное хранилище промптов для картинок (до выбора стиля)
# Формат: {user_id: "текст запроса"}
pending_image_prompts: dict[int, str] = {}

# ---------- БД ----------
def get_conn() -> sqlite3.Connection:
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
            ref_bonus_paid INTEGER DEFAULT 0,
            last_bonus_time TEXT DEFAULT '',
            is_banned INTEGER DEFAULT 0
        )
    """)
    try:
        conn.execute("ALTER TABLE users ADD COLUMN is_banned INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    return conn

def get_user(user_id: int, ref_id: int = 0) -> tuple[tuple[int, int], str]:
    with closing(get_conn()) as conn:
        cur = conn.execute(
            "SELECT text_balance, img_balance, last_bonus_time FROM users WHERE user_id = ?",
            (user_id,),
        )
        row = cur.fetchone()
        now = datetime.now()
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
        
        if row:
            last_time_str = row[2]
            give_bonus = False
            
            if not last_time_str:
                give_bonus = True
            else:
                try:
                    last_time = datetime.strptime(last_time_str, "%Y-%m-%d %H:%M:%S")
                    if (now - last_time) > timedelta(hours=24):
                        give_bonus = True
                except ValueError:
                    give_bonus = True

            if give_bonus:
                conn.execute("UPDATE users SET text_balance = text_balance + 1, last_bonus_time = ? WHERE user_id = ?", (now_str, user_id))
                conn.commit()
                return (row[0] + 1, row[1]), "daily_bonus"
            
            return (row[0], row[1]), "existing"

        # Новый пользователь
        valid_ref = 0
        if ref_id and ref_id != user_id:
            cur = conn.execute("SELECT 1 FROM users WHERE user_id = ?", (ref_id,))
            if cur.fetchone():
                valid_ref = ref_id

        conn.execute(
            "INSERT INTO users (user_id, text_balance, img_balance, referred_by, last_bonus_time) "
            "VALUES (?, 5, 1, ?, ?)",
            (user_id, valid_ref, now_str),
        )
        conn.commit()
        return (5, 1), "new"

def update_balance(user_id: int, text_delta: int = 0, img_delta: int = 0) -> None:
    with closing(get_conn()) as conn:
        # Защита от ухода в минус
        if text_delta < 0:
            conn.execute(
                "UPDATE users SET text_balance = MAX(0, text_balance + ?) WHERE user_id = ?",
                (text_delta, user_id),
            )
        if img_delta < 0:
            conn.execute(
                "UPDATE users SET img_balance = MAX(0, img_balance + ?) WHERE user_id = ?",
                (img_delta, user_id),
            )
        conn.execute(
            "UPDATE users SET text_balance = text_balance + ?, img_balance = img_balance + ? WHERE user_id = ?",
            (text_delta if text_delta > 0 else 0, img_delta if img_delta > 0 else 0, user_id),
        )
        conn.commit()

def try_consume(user_id: int, kind: str, amount: int = 1) -> bool:
    col = "text_balance" if kind == "text" else "img_balance"
    with closing(get_conn()) as conn:
        cur = conn.execute(
            f"UPDATE users SET {col} = {col} - ? WHERE user_id = ? AND {col} >= ?",
            (amount, user_id, amount),
        )
        conn.commit()
        return cur.rowcount > 0

def check_and_pay_referral(user_id: int):
    with closing(get_conn()) as conn:
        cur = conn.execute("SELECT referred_by, has_made_request, ref_bonus_paid FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        if row and row[0] != 0 and row[1] == 0 and row[2] == 0:
            ref_id = row[0]
            conn.execute("UPDATE users SET has_made_request = 1, ref_bonus_paid = 1 WHERE user_id = ?", (user_id,))
            conn.execute("UPDATE users SET text_balance = text_balance + 5, img_balance = img_balance + 1 WHERE user_id = ?", (ref_id,))
            conn.commit()
            return ref_id
    return None

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

# Новая клавиатура для выбора стиля картинки
style_kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="📸 Фотореализм", callback_data="style_realism")],
    [InlineKeyboardButton(text="🎌 Аниме", callback_data="style_anime")],
    [InlineKeyboardButton(text="🧊 3D-Графика", callback_data="style_3d")],
    [InlineKeyboardButton(text="🌆 Киберпанк", callback_data="style_cyberpunk")],
    [InlineKeyboardButton(text="🎨 Без стиля (как есть)", callback_data="style_none")]
])

# ---------- Утилиты ----------
def split_text(text: str, limit: int = 4000) -> list[str]:
    return [text[i:i + limit] for i in range(0, len(text), limit)] or [""]

async def safe_send_text(message: types.Message, text: str) -> None:
    for chunk in split_text(text):
        try:
            await message.answer(chunk)
        except TelegramBadRequest:
            await message.answer(html.escape(chunk), parse_mode=None)

async def safe_delete(message: types.Message | None) -> None:
    if not message: return
    try: await message.delete()
    except TelegramBadRequest: pass

def is_admin(user_id: int) -> bool:
    return ADMIN_ID != 0 and user_id == ADMIN_ID

# ---------- Хендлеры ----------
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    args = message.text.split(maxsplit=1)
    ref_id = int(args[1]) if len(args) > 1 and args[1].isdigit() else 0

    (text_bal, img_bal), status = get_user(message.from_user.id, ref_id)
    user_modes[message.from_user.id] = "fast"
    pending_image_prompts.pop(message.from_user.id, None)

    bonus_text = ""
    if status == "daily_bonus":
        bonus_text = "\n\n🎁 *Ежедневный бонус:* +1 текстовый запрос!"
    elif status == "new" and ref_id and ref_id != message.from_user.id:
        bonus_text = "\n\n🤝 *Вы пришли по ссылке друга!* Сделайте первый запрос, и он получит бонус."

    await message.answer(
        "👋 *Привет! Я твой универсальный ИИ-помощник.*\n\n"
        "Здесь собраны лучшие нейросети мира — *без зарубежных карт и сложных настроек*.\n\n"
        f"🎁 Твой баланс: *{text_bal} текстовых запросов* и *{img_bal} генераций картинок*."
        f"{bonus_text}\n\n"
        "👇 Выбери режим в меню ниже и просто напиши свой запрос:",
        reply_markup=main_kb, parse_mode="Markdown",
    )

@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if not is_admin(message.from_user.id): return
    with closing(get_conn()) as conn:
        cur = conn.execute("SELECT COUNT(*), COALESCE(SUM(text_balance), 0), COALESCE(SUM(img_balance), 0) FROM users")
        total_users, total_texts, total_imgs = cur.fetchone()
    await message.answer(
        "👑 *Панель администратора*\n\n"
        f"👥 Всего пользователей: *{total_users}*\n"
        f"💬 Остаток текстов: *{total_texts}*\n"
        f"🎨 Остаток картинок: *{total_imgs}*\n\n"
        "➕ Начислить: `/add ID тексты картинки`\n"
        "➖ Забрать: `/take ID тексты картинки`\n"
        "👤 Инфо: `/info ID`\n"
        "🚫 Бан: `/ban ID`\n"
        "✅ Разбан: `/unban ID`\n"
        "🏆 Топ: `/top`\n"
        "📢 Рассылка: `/broadcast Текст`",
        parse_mode="Markdown",
    )

@dp.message(Command("add"))
async def cmd_add_balance(message: types.Message):
    if not is_admin(message.from_user.id): return
    parts = message.text.split()
    if len(parts) != 4:
        await message.answer("⚠️ Формат: `/add ID тексты картинки`", parse_mode="Markdown")
        return
    try:
        target_id, add_t, add_i = int(parts[1]), int(parts[2]), int(parts[3])
    except ValueError:
        await message.answer("⚠️ ID и числа должны быть целыми.")
        return
    get_user(target_id)
    update_balance(target_id, text_delta=add_t, img_delta=add_i)
    await message.answer(f"✅ Пользователю `{target_id}` начислено +{add_t} текстов и +{add_i} картинок.", parse_mode="Markdown")
    try:
        await bot.send_message(target_id, f"🎁 Администратор начислил тебе *+{add_t} текстов* и *+{add_i} картинок*!", parse_mode="Markdown")
    except Exception: pass

@dp.message(Command("take"))
async def cmd_take_balance(message: types.Message):
    if not is_admin(message.from_user.id): return
    parts = message.text.split()
    if len(parts) != 4:
        await message.answer("⚠️ Формат: `/take ID тексты картинки`", parse_mode="Markdown")
        return
    try:
        target_id, take_t, take_i = int(parts[1]), int(parts[2]), int(parts[3])
    except ValueError:
        await message.answer("⚠️ ID и числа должны быть целыми.")
        return
    update_balance(target_id, text_delta=-take_t, img_delta=-take_i)
    await message.answer(f"✅ У пользователя `{target_id}` списано {take_t} текстов и {take_i} картинок.", parse_mode="Markdown")

@dp.message(Command("info"))
async def cmd_info(message: types.Message):
    if not is_admin(message.from_user.id): return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("⚠️ Формат: `/info ID_пользователя`", parse_mode="Markdown")
        return
    try:
        target_id = int(parts[1])
    except ValueError:
        await message.answer("⚠️ ID должен быть числом.")
        return
    with closing(get_conn()) as conn:
        cur = conn.execute("SELECT text_balance, img_balance, has_made_request, is_banned FROM users WHERE user_id = ?", (target_id,))
        row = cur.fetchone()
    if not row:
        await message.answer("❌ Пользователь не найден в базе.")
        return
    await message.answer(
        f"👤 *Информация о пользователе `{target_id}`*\n\n"
        f"💬 Текстов: *{row[0]}*\n"
        f"🎨 Картинок: *{row[1]}*\n"
        f"✅ Сделал запрос: {'Да' if row[2] else 'Нет'}\n"
        f"🚫 Забанен: {'Да' if row[3] else 'Нет'}",
        parse_mode="Markdown"
    )

@dp.message(Command("ban"))
async def cmd_ban(message: types.Message):
    if not is_admin(message.from_user.id): return
    parts = message.text.split()
    if len(parts) != 2: return
    try: target_id = int(parts[1])
    except ValueError: return
    with closing(get_conn()) as conn:
        conn.execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (target_id,))
        conn.commit()
    await message.answer(f"🚫 Пользователь `{target_id}` забанен.", parse_mode="Markdown")

@dp.message(Command("unban"))
async def cmd_unban(message: types.Message):
    if not is_admin(message.from_user.id): return
    parts = message.text.split()
    if len(parts) != 2: return
    try: target_id = int(parts[1])
    except ValueError: return
    with closing(get_conn()) as conn:
        conn.execute("UPDATE users SET is_banned = 0 WHERE user_id = ?", (target_id,))
        conn.commit()
    await message.answer(f"✅ Пользователь `{target_id}` разбанен.", parse_mode="Markdown")

@dp.message(Command("top"))
async def cmd_top(message: types.Message):
    if not is_admin(message.from_user.id): return
    with closing(get_conn()) as conn:
        cur = conn.execute("SELECT user_id, text_balance, img_balance FROM users ORDER BY (text_balance + img_balance) DESC LIMIT 10")
        rows = cur.fetchall()
    if not rows:
        await message.answer("База пуста.")
        return
    text = "🏆 *Топ-10 пользователей по балансу:*\n\n"
    for i, row in enumerate(rows, 1):
        text += f"{i}. `{row[0]}` | 💬 {row[1]} | 🎨 {row[2]}\n"
    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message):
    if not is_admin(message.from_user.id): return
    text_to_send = message.text.replace("/broadcast", "").strip()
    if not text_to_send:
        await message.answer("⚠️ Напишите текст после команды. Пример:\n`/broadcast Всем привет!`", parse_mode="Markdown")
        return
    await message.answer("⏳ Начинаю рассылку...")
    with closing(get_conn()) as conn:
        cur = conn.execute("SELECT user_id FROM users WHERE is_banned = 0")
        users = cur.fetchall()
    success, failed = 0, 0
    for (user_id,) in users:
        try:
            await bot.send_message(user_id, text_to_send)
            success += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1
    await message.answer(f"✅ *Рассылка завершена!*\n\nДоставлено: {success}\nОшибок: {failed}", parse_mode="Markdown")


@dp.message(F.text == "✍️ Написать / Решить задачу")
async def mode_fast(message: types.Message):
    user_modes[message.from_user.id] = "fast"
    pending_image_prompts.pop(message.from_user.id, None)
    await message.answer("✍️ Режим *Быстрый текст* включён. Напиши вопрос или отправь голосовое:", parse_mode="Markdown")

@dp.message(F.text == "🧠 Сложный анализ / Код")
async def mode_smart(message: types.Message):
    user_modes[message.from_user.id] = "smart"
    pending_image_prompts.pop(message.from_user.id, None)
    await message.answer("🧠 Режим *Глубокий анализ* включён.\n⚠️ Списывает *5 запросов* за ответ.", parse_mode="Markdown")

@dp.message(F.text == "🎨 Создать картинку")
async def mode_image(message: types.Message):
    (_, img_bal), _ = get_user(message.from_user.id)
    if img_bal <= 0:
        await message.answer("❌ Закончились генерации картинок.")
        user_modes[message.from_user.id] = "fast"
        return
    user_modes[message.from_user.id] = "image"
    pending_image_prompts.pop(message.from_user.id, None)
    await message.answer("🎨 Режим *Генерация картинок* включён. Опиши, что нарисовать:", parse_mode="Markdown")

@dp.message(F.text == "👤 Мой профиль / Баланс")
async def show_profile(message: types.Message):
    (text_bal, img_bal), status = get_user(message.from_user.id)
    bonus_text = "\n\n🎁 *Ежедневный бонус:* +1 запрос!" if status == "daily_bonus" else ""
    await message.answer(
        f"👤 *Профиль (ID: `{message.from_user.id}`)*\n\n"
        f"💬 Текстовых запросов: *{text_bal}*\n"
        f"🎨 Картинок: *{img_bal}*{bonus_text}",
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
        "Бонус (+5 текстов, +1 картинка) начислится, когда друг сделает первый запрос:\n\n"
        f"`{ref_link}`", parse_mode="Markdown",
    )

@dp.message(F.text == "💎 Купить подписку")
async def show_tariffs(message: types.Message):
    await message.answer("💎 *Выбери способ пополнения:*", reply_markup=buy_menu_kb, parse_mode="Markdown")

@dp.callback_query(F.data == "menu_packs")
async def cb_menu_packs(callback: types.CallbackQuery):
    await callback.message.edit_text("📦 *Выбери пакет:*\nОплата в Telegram Stars (⭐).", reply_markup=packs_kb, parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "menu_single")
async def cb_menu_single(callback: types.CallbackQuery):
    await callback.message.edit_text("🛒 *Купить поштучно:*", reply_markup=single_kb, parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "menu_back")
async def cb_menu_back(callback: types.CallbackQuery):
    await callback.message.edit_text("💎 *Выбери способ пополнения:*", reply_markup=buy_menu_kb, parse_mode="Markdown")
    await callback.answer()

async def send_invoice(chat_id: int, title: str, description: str, payload: str, stars_amount: int):
    await bot.send_invoice(chat_id=chat_id, title=title, description=description, payload=payload, provider_token="", currency="XTR", prices=[LabeledPrice(label=title, amount=stars_amount)])

@dp.callback_query(F.data.startswith("buy_"))
async def cb_buy(callback: types.CallbackQuery):
    data = callback.data
    if data == "buy_pack_start": await send_invoice(callback.message.chat.id, "Тариф Старт", "100 текстов + 10 картинок", data, 200)
    elif data == "buy_pack_opt": await send_invoice(callback.message.chat.id, "Тариф Оптимальный", "300 текстов + 35 картинок", data, 550)
    elif data == "buy_pack_pro": await send_invoice(callback.message.chat.id, "Тариф Профи", "1000 текстов + 80 картинок", data, 1500)
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

# ---------- ОБРАБОТКА КОНТЕНТА ----------
@dp.message(F.text & ~F.text.startswith("/"))
async def handle_text(message: types.Message):
    user_id = message.from_user.id
    
    with closing(get_conn()) as conn:
        cur = conn.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        if row and row[0] == 1:
            await message.answer("⛔ Вы заблокированы в этом боте.")
            return

    (text_bal, img_bal), status = get_user(user_id)
    if status == "daily_bonus":
        await message.answer("🎁 *Ежедневный бонус:* Тебе начислен +1 текстовый запрос!", parse_mode="Markdown")

    mode = user_modes.get(user_id, "fast")

    ref_paid_to = check_and_pay_referral(user_id)
    if ref_paid_to:
        try: await bot.send_message(ref_paid_to, "🤝 Твой друг сделал первый запрос! Тебе начислено *+5 текстов* и *+1 картинка*.", parse_mode="Markdown")
        except: pass

    if mode == "image":
        # Вместо генерации сразу, мы сохраняем промпт и просим выбрать стиль
        if img_bal <= 0:
            await message.answer("❌ Закончились генерации.")
            user_modes[user_id] = "fast" 
            return
        
        pending_image_prompts[user_id] = message.text
        await message.answer(
            "🎨 Отлично! Теперь выбери стиль для твоей картинки (это бесплатно):",
            reply_markup=style_kb
        )
        return

    cost = 5 if mode == "smart" else 1
    if not try_consume(user_id, "text", amount=cost):
        await message.answer(f"❌ Недостаточно запросов (нужно {cost}).")
        return

    selected_model = MODEL_TEXT_SMART if mode == "smart" else MODEL_TEXT_FAST
    wait_msg = await message.answer("⏳ Думаю...")
    try:
        response = await ai_client.chat.completions.create(model=selected_model, messages=[{"role": "user", "content": message.text}], max_tokens=2000)
        await safe_delete(wait_msg)
        await safe_send_text(message, response.choices[0].message.content)
    except Exception as e:
        update_balance(user_id, text_delta=cost)
        await safe_delete(wait_msg)
        await message.answer(f"⚠️ Ошибка: {str(e)[:100]}")

# ---------- ОБРАБОТКА ВЫБОРА СТИЛЯ ----------
@dp.callback_query(F.data.startswith("style_"))
async def process_style_choice(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    prompt = pending_image_prompts.pop(user_id, None)

    if not prompt:
        await callback.message.edit_text("⚠️ Запрос устарел. Пожалуйста, нажми '🎨 Создать картинку' и напиши запрос заново.")
        return

    # Формируем финальный промпт в зависимости от стиля
    style = callback.data
    if style == "style_realism":
        final_prompt = f"{prompt}, фотореализм, 8k, высокое качество, детализированное фото"
    elif style == "style_anime":
        final_prompt = f"{prompt}, аниме стиль, 2d, студия гибли, яркие цвета"
    elif style == "style_3d":
        final_prompt = f"{prompt}, 3d рендер, pixar, blender, высокое качество"
    elif style == "style_cyberpunk":
        final_prompt = f"{prompt}, киберпанк стиль, неон, футуризм, 8k"
    else:
        final_prompt = prompt # Без стиля

    await callback.message.edit_text(f"🎨 Рисую в выбранном стиле...")

    if not try_consume(user_id, "img"):
        await callback.message.edit_text("❌ Закончились генерации.")
        user_modes[user_id] = "fast"
        return

    try:
        response = await ai_client.images.generate(model=MODEL_IMAGE, prompt=final_prompt)
        image_data = response.data[0]
        
        (_, img_bal), _ = get_user(user_id)
        
        if getattr(image_data, 'url', None):
            await callback.message.answer_photo(photo=image_data.url, caption=f"✅ Готово! Осталось: {img_bal}")
        elif getattr(image_data, 'b64_json', None):
            image_bytes = base64.b64decode(image_data.b64_json)
            photo_file = BufferedInputFile(image_bytes, filename="image.png")
            await callback.message.answer_photo(photo=photo_file, caption=f"✅ Готово! Осталось: {img_bal}")
        else:
            raise ValueError("API не вернул ни URL, ни base64 данные.")

    except Exception as e:
        update_balance(user_id, img_delta=1) # Возврат баланса
        await callback.message.answer(f"⚠️ Ошибка генерации: {str(e)[:100]}")
    
    await callback.answer()

@dp.message(F.voice)
async def handle_voice(message: types.Message):
    user_id = message.from_user.id
    
    with closing(get_conn()) as conn:
        cur = conn.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        if row and row[0] == 1:
            await message.answer("⛔ Вы заблокированы в этом боте.")
            return

    if not try_consume(user_id, "text", amount=2):
        await message.answer("❌ Закончились запросы (для голоса нужно 2).")
        return

    ref_paid_to = check_and_pay_referral(user_id)
    if ref_paid_to:
        try: await bot.send_message(ref_paid_to, "🤝 Твой друг сделал первый запрос! Тебе начислено *+5 текстов* и *+1 картинка*.", parse_mode="Markdown")
        except: pass

    wait_msg = await message.answer("🎤 Распознаю...")
    try:
        file_info = await bot.get_file(message.voice.file_id)
        file_bytes = await bot.download_file(file_info.file_path)
        transcript = await ai_client.audio.transcriptions.create(model=MODEL_VOICE, file=("voice.ogg", file_bytes.read()))
        text_query = transcript.text
        if not text_query: raise ValueError("Не распознано")
        await wait_msg.edit_text(f"🗣️ *Вы сказали:* {text_query}\n\n⏳ Думаю...", parse_mode="Markdown")
        response = await ai_client.chat.completions.create(model=MODEL_TEXT_FAST, messages=[{"role": "user", "content": text_query}], max_tokens=2000)
        await safe_delete(wait_msg)
        await safe_send_text(message, response.choices[0].message.content)
    except Exception as e:
        update_balance(user_id, text_delta=2)
        await safe_delete(wait_msg)
        await message.answer(f"⚠️ Ошибка: {str(e)[:100]}")

@dp.message(F.photo | F.document)
async def handle_photo_doc(message: types.Message):
    await message.answer("⚠️ Я работаю только с текстом и голосом. Пожалуйста, напиши свой вопрос текстом или отправь голосовое сообщение!")

@dp.message()
async def fallback(message: types.Message):
    await message.answer("Отправь текст, голос или выбери режим в меню 👇", reply_markup=main_kb)

# ---------- Запуск ----------
async def main() -> None:
    with closing(get_conn()) as conn: pass
    global bot_username
    try:
        me = await bot.get_me()
        bot_username = me.username or ""
        logger.info("Bot started as @%s", bot_username)
    except Exception: pass

    await bot.delete_webhook(drop_pending_updates=True)
    try: await dp.start_polling(bot)
    finally:
        await ai_client.close()
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
