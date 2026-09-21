import asyncio
import hashlib
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl

import aiosqlite
import uvicorn
from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton,
    WebAppInfo, LabeledPrice, PreCheckoutQuery,
)
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x]
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://example.com")
DB_PATH = "casino.db"
COMMISSION = 0.05

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("casino")

# ==================== БД ====================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                balance INTEGER DEFAULT 0,
                total_topup INTEGER DEFAULT 0,
                total_spent INTEGER DEFAULT 0,
                referrer_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount INTEGER,
                type TEXT,
                description TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS rounds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game TEXT,
                pool INTEGER,
                winner_id INTEGER,
                payout INTEGER,
                commission INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                round_id INTEGER,
                user_id INTEGER,
                stake INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()

async def get_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as c:
            return await c.fetchone()

async def upsert_user(user_id: int, username: str, first_name: str, referrer_id: int | None = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO users (user_id, username, first_name, referrer_id)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 username = excluded.username,
                 first_name = excluded.first_name""",
            (user_id, username or "", first_name or "", referrer_id)
        )
        await db.commit()

async def change_balance(user_id: int, amount: int, ttype: str, desc: str = ""):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
        await db.execute(
            "INSERT INTO transactions (user_id, amount, type, description) VALUES (?,?,?,?)",
            (user_id, amount, ttype, desc)
        )
        await db.commit()

# ==================== ПРОВЕРКА initData ====================
def verify_init_data(init_data: str) -> dict:
    """Возвращает данные пользователя или бросает 401."""
    parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise HTTPException(401, "no hash")

    data_check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected, received_hash):
        raise HTTPException(401, "invalid hash")

    user = json.loads(parsed.get("user", "{}"))
    if not user.get("id"):
        raise HTTPException(401, "no user")
    return user

# ==================== BOT ====================
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()

@router.message(CommandStart())
async def cmd_start(message: Message):
    # Реф-код: /start ref_12345
    referrer = None
    if message.text and len(message.text.split()) > 1:
        arg = message.text.split()[1]
        if arg.startswith("ref_") and arg[4:].isdigit():
            referrer = int(arg[4:])
            if referrer == message.from_user.id:
                referrer = None  # сам себя не пригласишь

    await upsert_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name,
        referrer,
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎰 Открыть казино", web_app=WebAppInfo(url=WEBAPP_URL))],
        [InlineKeyboardButton(text="💰 Пополнить", callback_data="topup")],
    ])
    await message.answer(
        f"👋 Привет, <b>{message.from_user.full_name}</b>!\n\n"
        f"💫 Играй в PvP-игры, участвуй в раундах и забирай банк.\n"
        f"Открой казино кнопкой ниже 👇",
        reply_markup=kb,
    )

@router.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    await q.answer(ok=True)

@router.message(F.successful_payment)
async def on_payment(message: Message):
    amount = message.successful_payment.total_amount
    await change_balance(message.from_user.id, amount, "topup", f"Пополнение {amount}⭐")
    # Прибавим к total_topup
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET total_topup = total_topup + ? WHERE user_id = ?",
                         (amount, message.from_user.id))
        await db.commit()
    await message.answer(f"✅ Пополнено <b>+{amount}⭐</b>")

dp.include_router(router)

# ==================== FASTAPI ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    log.info("✅ БД готова")
    # запуск бота в фоне
    bot_task = asyncio.create_task(dp.start_polling(bot))
    log.info("🤖 Бот запущен")
    yield
    bot_task.cancel()

app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def root():
    return FileResponse("index.html")

app.mount("/static", StaticFiles(directory="."), name="static")

# --------- API ---------
@app.get("/api/me")
async def api_me(request: Request):
    init_data = request.headers.get("X-Init-Data")
    if not init_data:
        raise HTTPException(401, "no init data")
    user = verify_init_data(init_data)
    db_user = await get_user(user["id"])
    if not db_user:
        await upsert_user(user["id"], user.get("username", ""), user.get("first_name", ""))
        db_user = await get_user(user["id"])
    return {
        "user_id": user["id"],
        "username": user.get("username", ""),
        "first_name": user.get("first_name", ""),
        "balance": db_user["balance"],
        "total_topup": db_user["total_topup"],
        "total_spent": db_user["total_spent"],
    }

@app.get("/api/history")
async def api_history(request: Request):
    init_data = request.headers.get("X-Init-Data")
    if not init_data:
        raise HTTPException(401)
    user = verify_init_data(init_data)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM transactions WHERE user_id = ? ORDER BY id DESC LIMIT 20",
            (user["id"],)
        ) as c:
            rows = await c.fetchall()
    return [dict(r) for r in rows]

# --------- Пополнение (создание инвойса) ---------
@app.post("/api/topup")
async def api_topup(request: Request):
    init_data = request.headers.get("X-Init-Data")
    if not init_data:
        raise HTTPException(401)
    user = verify_init_data(init_data)
    body = await request.json()
    amount = int(body.get("amount", 0))
    if amount < 50 or amount > 100000:
        raise HTTPException(400, "Сумма от 50 до 100000⭐")

    # Отправляем инвойс пользователю в личку
    await bot.send_invoice(
        chat_id=user["id"],
        title=f"Пополнение на {amount}⭐",
        description=f"Зачислит {amount} звёзд на баланс",
        payload=f"topup:{amount}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=f"{amount} звёзд", amount=amount)],
    )
    return {"ok": True, "message": "Инвойс отправлен в чат с ботом"}

# ==================== ЗАПУСК ====================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("bot:app", host="0.0.0.0", port=port, reload=False)