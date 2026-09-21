import asyncio
import hashlib
import hmac
import json
import logging
import os
import random
import time
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
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x]
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://example.com")
DB_PATH = "casino.db"

COMMISSION = 0.05            # 5% боту
MIN_BET = 10                 # минимум 10⭐
MAX_BET = 10000              # максимум 10000⭐
MAX_PLAYERS = 10             # максимум игроков в раунде
LOBBY_TIME = 30              # 30 секунд на приём ставок
SPIN_TIME = 6                # 6 секунд на анимацию

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
                winner_name TEXT,
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
                username TEXT,
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

async def atomic_bet(user_id: int, amount: int) -> bool:
    """Атомарно списываем ставку. Возвращает True если успешно."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE users SET balance = balance - ? WHERE user_id = ? AND balance >= ?",
            (amount, user_id, amount)
        )
        await db.commit()
        return cur.rowcount > 0

async def save_round(game: str, pool: int, winner_id: int, winner_name: str, payout: int, commission: int, bets: list):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO rounds (game, pool, winner_id, winner_name, payout, commission)
               VALUES (?,?,?,?,?,?)""",
            (game, pool, winner_id, winner_name, payout, commission)
        )
        round_id = cur.lastrowid
        for b in bets:
            await db.execute(
                "INSERT INTO bets (round_id, user_id, username, stake) VALUES (?,?,?,?)",
                (round_id, b["user_id"], b["username"], b["stake"])
            )
        await db.commit()
        return round_id

async def get_recent_rounds(limit: int = 10):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM rounds ORDER BY id DESC LIMIT ?", (limit,)
        ) as c:
            return [dict(r) for r in await c.fetchall()]

# ==================== initData ====================
def verify_init_data(init_data: str) -> dict:
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

# ==================== БОТ ====================
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()

@router.message(CommandStart())
async def cmd_start(message: Message):
    referrer = None
    if message.text and len(message.text.split()) > 1:
        arg = message.text.split()[1]
        if arg.startswith("ref_") and arg[4:].isdigit():
            referrer = int(arg[4:])
            if referrer == message.from_user.id:
                referrer = None
    await upsert_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name,
        referrer,
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎰 Открыть казино", web_app=WebAppInfo(url=WEBAPP_URL))],
    ])
    await message.answer(
        f"👋 Привет, <b>{message.from_user.full_name}</b>!\n\n"
        f"💫 PvP-игры: Колесо и Квадрат.\n"
        f"Ставки от {MIN_BET}⭐, победитель забирает 95% банка.\n\n"
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
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET total_topup = total_topup + ? WHERE user_id = ?",
                         (amount, message.from_user.id))
        await db.commit()
    await message.answer(f"✅ Пополнено <b>+{amount}⭐</b>")

dp.include_router(router)

# ==================== ИГРОВАЯ ЛОГИКА ====================
# Лобби каждой игры: {"wheel": {...}, "square": {...}}
lobbies = {
    "wheel":  {"players": [], "deadline": 0, "task": None, "spinning": False},
    "square": {"players": [], "deadline": 0, "task": None, "spinning": False},
}

# WebSocket-подключения: {user_id: WebSocket}
ws_clients: dict[int, WebSocket] = {}

def pick_winner(players: list) -> dict:
    """Пропорциональный выбор по ставкам."""
    total = sum(p["stake"] for p in players)
    roll = random.random() * total
    for p in players:
        roll -= p["stake"]
        if roll <= 0:
            return p
    return players[-1]

def build_state(game: str) -> dict:
    """Текущее состояние лобби для отправки клиентам."""
    lob = lobbies[game]
    total = sum(p["stake"] for p in lob["players"])
    now = time.time()
    return {
        "game": game,
        "players": [
            {
                "user_id": p["user_id"],
                "username": p["username"],
                "first_name": p["first_name"],
                "stake": p["stake"],
                "share": round(p["stake"] / total * 100, 2) if total else 0,
                "angle": round(p["stake"] / total * 360, 2) if total else 0,
                "color": p["color"],
            }
            for p in lob["players"]
        ],
        "pool": total,
        "time_left": max(0, int(lob["deadline"] - now)),
        "spinning": lob["spinning"],
        "players_count": len(lob["players"]),
    }

async def broadcast(game: str, event: str, payload: dict):
    """Отправить событие всем клиентам, кто в этом лобби или просто смотрит."""
    message = {"event": event, "data": payload}
    dead = []
    for uid, ws in list(ws_clients.items()):
        try:
            await ws.send_json(message)
        except Exception:
            dead.append(uid)
    for uid in dead:
        ws_clients.pop(uid, None)

async def broadcast_state(game: str):
    await broadcast(game, "state", build_state(game))

async def start_lobby_timer(game: str):
    """Запустить таймер лобби — по истечении стартует раунд."""
    lob = lobbies[game]
    if lob["task"] and not lob["task"].done():
        return
    lob["deadline"] = time.time() + LOBBY_TIME
    lob["task"] = asyncio.create_task(_lobby_countdown(game))

async def _lobby_countdown(game: str):
    """Тикаем каждую секунду, рассылаем таймер. По истечении — раунд."""
    lob = lobbies[game]
    try:
        while True:
            left = int(lob["deadline"] - time.time())
            await broadcast_state(game)
            if left <= 0:
                break
            await asyncio.sleep(1)
        # время вышло
        if len(lob["players"]) >= 2:
            await run_round(game)
        else:
            # возвращаем ставки
            for p in lob["players"]:
                await change_balance(p["user_id"], p["stake"], "refund", "Возврат (никто не зашёл)")
            players_backup = list(lob["players"])
            lob["players"] = []
            await broadcast(game, "refunded", {"players": players_backup})
            await broadcast_state(game)
    except asyncio.CancelledError:
        pass

async def run_round(game: str):
    """Определяем победителя, крутим анимацию, начисляем деньги."""
    lob = lobbies[game]
    players = list(lob["players"])
    lob["players"] = []
    lob["spinning"] = True

    total = sum(p["stake"] for p in players)
    winner = pick_winner(players)
    winner_index = players.index(winner)
    payout = int(total * (1 - COMMISSION))
    commission = total - payout

    # Старт анимации
    await broadcast(game, "spin", {
        "players": players,
        "winner_index": winner_index,
        "winner_id": winner["user_id"],
        "winner_name": winner["username"],
        "total": total,
        "payout": payout,
        "duration": SPIN_TIME,
    })

    # Дать клиентам время на анимацию
    await asyncio.sleep(SPIN_TIME)

    # Начисление
    await change_balance(winner["user_id"], payout, "win", f"Победа в {game} (+{payout}⭐)")
    for p in players:
        if p["user_id"] != winner["user_id"]:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("UPDATE users SET total_spent = total_spent + ? WHERE user_id = ?",
                                 (p["stake"], p["user_id"]))
                await db.commit()

    await save_round(game, total, winner["user_id"], winner["username"], payout, commission, players)

    await broadcast(game, "result", {
        "winner_id": winner["user_id"],
        "winner_name": winner["username"],
        "payout": payout,
        "total": total,
        "commission": commission,
    })

    lob["spinning"] = False

    # Если есть новые ставки за время спина — стартуем сразу
    if len(lob["players"]) >= 2:
        asyncio.create_task(start_lobby_timer(game))
    else:
        lob["deadline"] = 0
        await broadcast_state(game)

# ==================== FASTAPI ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    log.info("✅ БД готова")
    bot_task = asyncio.create_task(dp.start_polling(bot))
    log.info("🤖 Бот запущен")
    yield
    bot_task.cancel()

app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/")
async def root():
    return FileResponse("index.html")

app.mount("/static", StaticFiles(directory="."), name="static")

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
    verify_init_data(init_data)
    return await get_recent_rounds(10)

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
    await bot.send_invoice(
        chat_id=user["id"],
        title=f"Пополнение на {amount}⭐",
        description=f"Зачислит {amount} звёзд",
        payload=f"topup:{amount}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=f"{amount} звёзд", amount=amount)],
    )
    return {"ok": True, "message": "Инвойс отправлен в чат с ботом"}

# ==================== ИГРОВЫЕ API ====================
@app.get("/api/game/{game}/state")
async def api_game_state(game: str, request: Request):
    init_data = request.headers.get("X-Init-Data")
    if not init_data:
        raise HTTPException(401)
    verify_init_data(init_data)
    if game not in lobbies:
        raise HTTPException(404)
    return build_state(game)

@app.post("/api/game/{game}/bet")
async def api_game_bet(game: str, request: Request):
    init_data = request.headers.get("X-Init-Data")
    if not init_data:
        raise HTTPException(401)
    user = verify_init_data(init_data)
    if game not in lobbies:
        raise HTTPException(404)

    body = await request.json()
    amount = int(body.get("amount", 0))

    if amount < MIN_BET:
        raise HTTPException(400, f"Минимум {MIN_BET}⭐")
    if amount > MAX_BET:
        raise HTTPException(400, f"Максимум {MAX_BET}⭐")

    lob = lobbies[game]
    if lob["spinning"]:
        raise HTTPException(400, "Идёт раунд, подожди")
    if len(lob["players"]) >= MAX_PLAYERS:
        raise HTTPException(400, "Лобби заполнено")
    for p in lob["players"]:
        if p["user_id"] == user["id"]:
            raise HTTPException(400, "Ты уже сделал ставку")

    # Атомарно списываем
    ok = await atomic_bet(user["id"], amount)
    if not ok:
        raise HTTPException(400, "Недостаточно средств")

    # Цвет игрока
    colors = ["#f7b733", "#4fc3f7", "#81c784", "#e57373", "#ba68c8",
              "#ffb74d", "#4db6ac", "#9575cd", "#aed581", "#f06292"]
    color = colors[len(lob["players"]) % len(colors)]

    lob["players"].append({
        "user_id": user["id"],
        "username": user.get("username", ""),
        "first_name": user.get("first_name", ""),
        "stake": amount,
        "color": color,
    })

    # Логируем транзакцию списания
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO transactions (user_id, amount, type, description) VALUES (?,?,?,?)",
            (user["id"], -amount, f"{game}_bet", f"Ставка {game}")
        )
        await db.commit()

    # Запускаем таймер, если ещё не запущен
    if len(lob["players"]) == 1 or not lob["task"] or lob["task"].done():
        await start_lobby_timer(game)
    else:
        await broadcast_state(game)

    return {"ok": True, "state": build_state(game)}

@app.websocket("/ws/{user_id}")
async def ws_endpoint(websocket: WebSocket, user_id: int):
    await websocket.accept()
    ws_clients[user_id] = websocket
    log.info(f"🔌 WS подключён: {user_id}")
    try:
        # Отправляем начальное состояние обеих игр
        await websocket.send_json({"event": "state", "data": build_state("wheel")})
        await websocket.send_json({"event": "state", "data": build_state("square")})
        while True:
            # Просто держим соединение, ждём ping/pong
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning(f"WS error {user_id}: {e}")
    finally:
        ws_clients.pop(user_id, None)
        log.info(f"🔌 WS отключён: {user_id}")

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("bot:app", host="0.0.0.0", port=port, reload=False)
