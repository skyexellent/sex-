import asyncio
import hashlib
import hmac
import json
import logging
import os
import random
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from urllib.parse import parse_qsl

import asyncpg
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
DATABASE_URL = os.getenv("DATABASE_URL")

COMMISSION = 0.05
REF_BONUS = 0.10
MIN_BET = 10
MAX_BET = 10000
MAX_PLAYERS = 10
LOBBY_TIME = 30
SPIN_TIME = 6
MIN_WITHDRAW = 100
DAILY_BONUS = 10

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("casino")

# ==================== БД (PostgreSQL) ====================
pool: asyncpg.Pool | None = None

async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)

    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                balance BIGINT DEFAULT 0,
                total_topup BIGINT DEFAULT 0,
                total_spent BIGINT DEFAULT 0,
                total_won BIGINT DEFAULT 0,
                referrer_id BIGINT,
                ref_earned BIGINT DEFAULT 0,
                last_bonus TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                amount BIGINT,
                type TEXT,
                description TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS rounds (
                id SERIAL PRIMARY KEY,
                game TEXT,
                pool BIGINT,
                winner_id BIGINT,
                winner_name TEXT,
                payout BIGINT,
                commission BIGINT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS bets (
                id SERIAL PRIMARY KEY,
                round_id INTEGER,
                user_id BIGINT,
                username TEXT,
                stake BIGINT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS withdrawals (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                amount BIGINT,
                requisites TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    log.info("✅ БД готова")

async def get_user(user_id: int):
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)

async def upsert_user(user_id: int, username: str, first_name: str, referrer_id: int | None = None):
    async with pool.acquire() as conn:
        existing = await conn.fetchrow("SELECT user_id FROM users WHERE user_id = $1", user_id)
        if existing:
            await conn.execute(
                "UPDATE users SET username = $1, first_name = $2 WHERE user_id = $3",
                username or "", first_name or "", user_id,
            )
        else:
            await conn.execute(
                """INSERT INTO users (user_id, username, first_name, referrer_id)
                   VALUES ($1, $2, $3, $4)""",
                user_id, username or "", first_name or "", referrer_id,
            )

async def change_balance(user_id: int, amount: int, ttype: str, desc: str = ""):
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", amount, user_id)
            await conn.execute(
                "INSERT INTO transactions (user_id, amount, type, description) VALUES ($1,$2,$3,$4)",
                user_id, amount, ttype, desc,
            )

async def atomic_bet(user_id: int, amount: int) -> bool:
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE users SET balance = balance - $1 WHERE user_id = $2 AND balance >= $1",
            amount, user_id,
        )
        return result == "UPDATE 1"

async def save_round(game: str, pool_: int, winner_id: int, winner_name: str,
                     payout: int, commission: int, bets: list):
    async with pool.acquire() as conn:
        async with conn.transaction():
            round_id = await conn.fetchval(
                """INSERT INTO rounds (game, pool, winner_id, winner_name, payout, commission)
                   VALUES ($1,$2,$3,$4,$5,$6) RETURNING id""",
                game, pool_, winner_id, winner_name, payout, commission,
            )
            for b in bets:
                await conn.execute(
                    "INSERT INTO bets (round_id, user_id, username, stake) VALUES ($1,$2,$3,$4)",
                    round_id, b["user_id"], b["username"], b["stake"],
                )

async def get_recent_rounds(limit: int = 20):
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM rounds ORDER BY id DESC LIMIT $1", limit)
        return [dict(r) for r in rows]

async def get_top_players(limit: int = 10):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT user_id, username, first_name, total_won, balance
               FROM users ORDER BY total_won DESC LIMIT $1""", limit
        )
        return [dict(r) for r in rows]

async def get_referral_stats(user_id: int):
    async with pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM users WHERE referrer_id = $1", user_id)
        earned = await conn.fetchval("SELECT ref_earned FROM users WHERE user_id = $1", user_id)
        return {"count": count or 0, "earned": earned or 0}

async def try_daily_bonus(user_id: int):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT last_bonus FROM users WHERE user_id = $1", user_id)
        if not row:
            return {"ok": False, "msg": "Профиль не найден"}
        last = row["last_bonus"]
        if last and datetime.utcnow() - last < timedelta(hours=24):
            left = timedelta(hours=24) - (datetime.utcnow() - last)
            return {"ok": False, "msg": f"Бонус доступен через {left}"}
        await conn.execute(
            "UPDATE users SET balance = balance + $1, last_bonus = CURRENT_TIMESTAMP WHERE user_id = $2",
            DAILY_BONUS, user_id,
        )
        await conn.execute(
            "INSERT INTO transactions (user_id, amount, type, description) VALUES ($1,$2,$3,$4)",
            user_id, DAILY_BONUS, "daily", "Ежедневный бонус",
        )
        return {"ok": True, "amount": DAILY_BONUS}

# --- Вывод ---
async def create_withdrawal(user_id: int, amount: int, requisites: str):
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO withdrawals (user_id, amount, requisites) VALUES ($1,$2,$3) RETURNING id",
            user_id, amount, requisites,
        )

async def get_pending_withdrawals():
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM withdrawals WHERE status='pending' ORDER BY id")
        return [dict(r) for r in rows]

async def set_withdrawal_status(wid: int, status: str):
    async with pool.acquire() as conn:
        await conn.execute("UPDATE withdrawals SET status=$1 WHERE id=$2", status, wid)

async def get_withdrawal(wid: int):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM withdrawals WHERE id=$1", wid)
        return dict(row) if row else None

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

def require_admin(user_id: int):
    if user_id not in ADMIN_IDS:
        raise HTTPException(403, "admin only")

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
    await upsert_user(message.from_user.id, message.from_user.username,
                     message.from_user.full_name, referrer)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎰 Открыть казино", web_app=WebAppInfo(url=WEBAPP_URL))],
    ])
    await message.answer(
        f"👋 Привет, <b>{message.from_user.full_name}</b>!\n\n"
        f"🎡 Колесо и 🟦 Квадрат — PvP-игры.\n"
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
    uid = message.from_user.id
    await change_balance(uid, amount, "topup", f"Пополнение {amount}⭐")
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET total_topup = total_topup + $1 WHERE user_id = $2", amount, uid)
        # реферальный бонус
        user = await conn.fetchrow("SELECT referrer_id FROM users WHERE user_id = $1", uid)
        if user and user["referrer_id"]:
            bonus = int(amount * REF_BONUS)
            if bonus > 0:
                await conn.execute(
                    "UPDATE users SET balance = balance + $1, ref_earned = ref_earned + $1 WHERE user_id = $2",
                    bonus, user["referrer_id"],
                )
                await conn.execute(
                    "INSERT INTO transactions (user_id, amount, type, description) VALUES ($1,$2,$3,$4)",
                    user["referrer_id"], bonus, "ref_bonus", f"Бонус за друга {uid}",
                )
                try:
                    await bot.send_message(
                        user["referrer_id"],
                        f"🎁 Реферальный бонус <b>+{bonus}⭐</b> за пополнение друга!"
                    )
                except Exception:
                    pass
    await message.answer(f"✅ Пополнено <b>+{amount}⭐</b>")

dp.include_router(router)

# ==================== ЛОББИ ИГР ====================
lobbies = {
    "wheel":  {"players": [], "deadline": 0, "task": None, "spinning": False},
    "square": {"players": [], "deadline": 0, "task": None, "spinning": False},
}
ws_clients: dict[int, WebSocket] = {}

def pick_winner(players: list) -> dict:
    total = sum(p["stake"] for p in players)
    roll = random.random() * total
    for p in players:
        roll -= p["stake"]
        if roll <= 0:
            return p
    return players[-1]

def build_state(game: str) -> dict:
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
    lob = lobbies[game]
    if lob["task"] and not lob["task"].done():
        return
    lob["deadline"] = time.time() + LOBBY_TIME
    lob["task"] = asyncio.create_task(_lobby_countdown(game))

async def _lobby_countdown(game: str):
    lob = lobbies[game]
    try:
        while True:
            left = int(lob["deadline"] - time.time())
            await broadcast_state(game)
            if left <= 0:
                break
            await asyncio.sleep(1)
        if len(lob["players"]) >= 2:
            await run_round(game)
        else:
            for p in lob["players"]:
                await change_balance(p["user_id"], p["stake"], "refund", "Возврат (никто не зашёл)")
            players_backup = list(lob["players"])
            lob["players"] = []
            await broadcast(game, "refunded", {"players": players_backup})
            await broadcast_state(game)
    except asyncio.CancelledError:
        pass

async def run_round(game: str):
    lob = lobbies[game]
    players = list(lob["players"])
    lob["players"] = []
    lob["spinning"] = True

    total = sum(p["stake"] for p in players)
    winner = pick_winner(players)
    winner_index = players.index(winner)
    payout = int(total * (1 - COMMISSION))
    commission = total - payout

    await broadcast(game, "spin", {
        "players": players,
        "winner_index": winner_index,
        "winner_id": winner["user_id"],
        "winner_name": winner["username"] or winner["first_name"],
        "total": total,
        "payout": payout,
        "duration": SPIN_TIME,
    })

    await asyncio.sleep(SPIN_TIME)

    await change_balance(winner["user_id"], payout, "win", f"Победа в {game} (+{payout}⭐)")
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET total_won = total_won + $1 WHERE user_id = $2",
                           payout, winner["user_id"])
        for p in players:
            if p["user_id"] != winner["user_id"]:
                await conn.execute("UPDATE users SET total_spent = total_spent + $1 WHERE user_id = $2",
                                   p["stake"], p["user_id"])

    await save_round(game, total, winner["user_id"],
                     winner["username"] or winner["first_name"],
                     payout, commission, players)

    await broadcast(game, "result", {
        "winner_id": winner["user_id"],
        "winner_name": winner["username"] or winner["first_name"],
        "payout": payout,
        "total": total,
        "commission": commission,
    })

    lob["spinning"] = False
    if len(lob["players"]) >= 2:
        asyncio.create_task(start_lobby_timer(game))
    else:
        lob["deadline"] = 0
        await broadcast_state(game)

# ==================== FASTAPI ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    bot_task = asyncio.create_task(dp.start_polling(bot))
    log.info("🤖 Бот запущен")
    yield
    bot_task.cancel()
    if pool:
        await pool.close()

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
    ref = await get_referral_stats(user["id"])
    return {
        "user_id": user["id"],
        "username": user.get("username", ""),
        "first_name": user.get("first_name", ""),
        "balance": db_user["balance"],
        "total_topup": db_user["total_topup"],
        "total_spent": db_user["total_spent"],
        "total_won": db_user["total_won"],
        "ref_count": ref["count"],
        "ref_earned": ref["earned"],
        "is_admin": user["id"] in ADMIN_IDS,
    }

@app.get("/api/history")
async def api_history(request: Request):
    init_data = request.headers.get("X-Init-Data")
    if not init_data:
        raise HTTPException(401)
    verify_init_data(init_data)
    return await get_recent_rounds(20)

@app.get("/api/top")
async def api_top(request: Request):
    init_data = request.headers.get("X-Init-Data")
    if not init_data:
        raise HTTPException(401)
    verify_init_data(init_data)
    return await get_top_players(10)

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

@app.post("/api/daily")
async def api_daily(request: Request):
    init_data = request.headers.get("X-Init-Data")
    if not init_data:
        raise HTTPException(401)
    user = verify_init_data(init_data)
    return await try_daily_bonus(user["id"])

# --------- Вывод ---------
@app.post("/api/withdraw")
async def api_withdraw(request: Request):
    init_data = request.headers.get("X-Init-Data")
    if not init_data:
        raise HTTPException(401)
    user = verify_init_data(init_data)
    body = await request.json()
    amount = int(body.get("amount", 0))
    requisites = (body.get("requisites") or "").strip()
    if amount < MIN_WITHDRAW:
        raise HTTPException(400, f"Минимум {MIN_WITHDRAW}⭐")
    if not requisites:
        raise HTTPException(400, "Укажи реквизиты")
    db_user = await get_user(user["id"])
    if db_user["balance"] < amount:
        raise HTTPException(400, "Недостаточно средств")
    ok = await atomic_bet(user["id"], amount)
    if not ok:
        raise HTTPException(400, "Недостаточно средств")
    await change_balance(user["id"], 0, "withdraw_hold", f"Заявка на вывод {amount}⭐")
    wid = await create_withdrawal(user["id"], amount, requisites)
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"💸 Заявка №{wid}\n👤 <code>{user['id']}</code> @{user.get('username','')}\n"
                f"💰 {amount}⭐\n📩 {requisites}",
            )
        except Exception:
            pass
    return {"ok": True, "withdrawal_id": wid}

# --------- Админка ---------
@app.get("/api/admin/stats")
async def api_admin_stats(request: Request):
    init_data = request.headers.get("X-Init-Data")
    if not init_data:
        raise HTTPException(401)
    user = verify_init_data(init_data)
    require_admin(user["id"])
    async with pool.acquire() as conn:
        users_count = await conn.fetchval("SELECT COUNT(*) FROM users")
        total_balance = await conn.fetchval("SELECT COALESCE(SUM(balance),0) FROM users")
        total_topup = await conn.fetchval("SELECT COALESCE(SUM(total_topup),0) FROM users")
        rounds_count = await conn.fetchval("SELECT COUNT(*) FROM rounds")
    return {
        "users": users_count, "total_balance": total_balance,
        "total_topup": total_topup, "rounds": rounds_count,
    }

@app.get("/api/admin/withdrawals")
async def api_admin_withdrawals(request: Request):
    init_data = request.headers.get("X-Init-Data")
    if not init_data:
        raise HTTPException(401)
    user = verify_init_data(init_data)
    require_admin(user["id"])
    return await get_pending_withdrawals()

@app.post("/api/admin/withdrawal/{wid}/done")
async def api_admin_wd_done(wid: int, request: Request):
    init_data = request.headers.get("X-Init-Data")
    if not init_data:
        raise HTTPException(401)
    user = verify_init_data(init_data)
    require_admin(user["id"])
    w = await get_withdrawal(wid)
    if not w or w["status"] != "pending":
        raise HTTPException(400, "Уже обработана")
    await set_withdrawal_status(wid, "done")
    try:
        await bot.send_message(w["user_id"], f"✅ Заявка №{wid} на {w['amount']}⭐ выполнена!")
    except Exception:
        pass
    return {"ok": True}

@app.post("/api/admin/set_balance")
async def api_admin_set_balance(request: Request):
    init_data = request.headers.get("X-Init-Data")
    if not init_data:
        raise HTTPException(401)
    user = verify_init_data(init_data)
    require_admin(user["id"])
    body = await request.json()
    target = int(body.get("user_id"))
    amount = int(body.get("amount"))
    db_user = await get_user(target)
    if not db_user:
        raise HTTPException(404, "Пользователь не найден")
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", amount, target)
        await conn.execute(
            "INSERT INTO transactions (user_id, amount, type, description) VALUES ($1,$2,$3,$4)",
            target, amount, "admin_edit", f"Админ {user['id']}: {amount:+}",
        )
    return {"ok": True, "new_balance": db_user["balance"] + amount}

# --------- Игры ---------
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
        raise HTTPException(400, "Идёт раунд")
    if len(lob["players"]) >= MAX_PLAYERS:
        raise HTTPException(400, "Лобби заполнено")
    for p in lob["players"]:
        if p["user_id"] == user["id"]:
            raise HTTPException(400, "Ты уже сделал ставку")
    if not await atomic_bet(user["id"], amount):
        raise HTTPException(400, "Недостаточно средств")

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
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO transactions (user_id, amount, type, description) VALUES ($1,$2,$3,$4)",
            user["id"], -amount, f"{game}_bet", f"Ставка {game}",
        )
    if len(lob["players"]) == 1 or not lob["task"] or lob["task"].done():
        await start_lobby_timer(game)
    else:
        await broadcast_state(game)
    return {"ok": True, "state": build_state(game)}

# --------- WebSocket ---------
@app.websocket("/ws/{user_id}")
async def ws_endpoint(websocket: WebSocket, user_id: int):
    await websocket.accept()
    ws_clients[user_id] = websocket
    try:
        await websocket.send_json({"event": "state", "data": build_state("wheel")})
        await websocket.send_json({"event": "state", "data": build_state("square")})
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning(f"WS error {user_id}: {e}")
    finally:
        ws_clients.pop(user_id, None)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("bot:app", host="0.0.0.0", port=port, reload=False)
