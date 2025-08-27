import os
import json
import asyncpg
import aiohttp
import csv
import io
import logging
import time, secrets, jwt
from types import SimpleNamespace
from urllib.parse import urlencode

from fastapi import FastAPI, Request, HTTPException, Depends, UploadFile, File
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from dotenv import load_dotenv
from contextlib import asynccontextmanager
from pydantic import BaseModel
from typing import List, Optional, Dict, Any

load_dotenv()

# --------- ENV ---------
required_env_vars = ["DATABASE_URL", "DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET", "DISCORD_REDIRECT_URI"]
missing_vars = [var for var in required_env_vars if not os.getenv(var)]
if missing_vars:
    raise ValueError(f"Missing required environment variables: {missing_vars}")

DATABASE_URL = os.getenv("DATABASE_URL")
PLAYER_DATABASE_URL = os.getenv("PLAYER_DATABASE_URL", "postgresql://postgres:FiwuZKPRyUKvzWMMqTWxfpRGtZrOYLCa@shuttle.proxy.rlwy.net:19669/railway")
WATCHLIST_DATABASE_URL = os.getenv("WATCHLIST_DATABASE_URL", DATABASE_URL)  # 👈 new (falls back to main DB)

DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI")
SECRET_KEY = os.getenv("SECRET_KEY")
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://frontend-production-ab5e.up.railway.app")
PORT = int(os.getenv("PORT", 8000))

# JWT / Discord
JWT_PRIVATE_KEY = os.getenv("JWT_PRIVATE_KEY", "dev-secret-change-me")
JWT_ISSUER = os.getenv("JWT_ISSUER", "fut-dashboard")
JWT_TTL_SECONDS = int(os.getenv("JWT_TTL_SECONDS", "2592000"))
DISCORD_OAUTH_AUTHORIZE = "https://discord.com/api/oauth2/authorize"
DISCORD_USERS_ME = "https://discord.com/api/users/@me"
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_SERVER_ID = os.getenv("DISCORD_SERVER_ID")
OAUTH_STATE = {}

if not SECRET_KEY:
    raise ValueError("SECRET_KEY environment variable is required")

# --------- FUT.GG / Watchlist config ---------
FUTGG_BASE = "https://www.fut.gg/api/fut/player-prices/25"
PRICE_CACHE_TTL = 5  # seconds
_price_cache: Dict[str, Dict[str, Any]] = {}  # {(card_id|platform): {"at": ts, "price": int|None, "isExtinct": bool, "updatedAt": str|None}}

# --------- MODELS ---------
class UserSettings(BaseModel):
    default_platform: Optional[str] = "Console"
    custom_tags: Optional[List[str]] = []
    currency_format: Optional[str] = "coins"  # coins, k, m
    theme: Optional[str] = "dark"
    timezone: Optional[str] = "UTC"
    date_format: Optional[str] = "US"  # US or EU
    include_tax_in_profit: Optional[bool] = True
    default_chart_range: Optional[str] = "30d"  # 7d, 30d, 90d, all
    visible_widgets: Optional[List[str]] = ["profit", "tax", "balance", "trades"]

class TradingGoal(BaseModel):
    title: str
    target_amount: int
    target_date: Optional[str] = None
    goal_type: str = "profit"
    is_completed: bool = False

class ExtSale(BaseModel):
    trade_id: int
    player_name: str
    card_version: Optional[str] = None
    buy_price: Optional[int] = None
    sell_price: int
    timestamp_ms: int

# Watchlist payload
class WatchlistCreate(BaseModel):
    card_id: int
    player_name: str
    version: Optional[str] = None
    platform: str  # "ps" | "xbox"
    notes: Optional[str] = None

# --------- POOLS & LIFESPAN ---------
pool = None               # main app DB
player_pool = None        # static players DB
watchlist_pool = None     # watchlist DB

@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool, player_pool, watchlist_pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    player_pool = await asyncpg.create_pool(PLAYER_DATABASE_URL, min_size=1, max_size=10)
    watchlist_pool = await asyncpg.create_pool(WATCHLIST_DATABASE_URL, min_size=1, max_size=10)

    # Create tables / indexes (main app)
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                id SERIAL PRIMARY KEY,
                user_id VARCHAR(255) UNIQUE NOT NULL,
                settings JSONB DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_profiles (
                id SERIAL PRIMARY KEY,
                user_id VARCHAR(255) UNIQUE NOT NULL,
                username VARCHAR(255),
                avatar_url TEXT,
                global_name VARCHAR(255),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS trading_goals (
                id SERIAL PRIMARY KEY,
                user_id VARCHAR(255) NOT NULL,
                title VARCHAR(255) NOT NULL,
                target_amount INTEGER NOT NULL,
                target_date DATE,
                goal_type VARCHAR(50) DEFAULT 'profit',
                is_completed BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP
            )
        """)
        await conn.execute("ALTER TABLE trades ADD COLUMN IF NOT EXISTS trade_id BIGINT")
        await conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS trades_user_trade_uidx ON trades (user_id, trade_id)""")
        await conn.execute("DROP INDEX IF EXISTS idx_trades_date")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_user_ts ON trades(user_id, timestamp)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_tag ON trades(user_id, tag)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_platform ON trades(user_id, platform)")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS fut_trades (
              id           BIGSERIAL PRIMARY KEY,
              discord_id   TEXT NOT NULL,
              trade_id     BIGINT NOT NULL,
              player_name  TEXT NOT NULL,
              card_version TEXT,
              buy_price    INTEGER,
              sell_price   INTEGER NOT NULL,
              ts           TIMESTAMP WITH TIME ZONE NOT NULL,
              source       TEXT DEFAULT 'webapp'
            )
        """)
        await conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS fut_trades_uidx ON fut_trades (discord_id, trade_id)""")

    # Create tables / indexes (watchlist DB)
    async with watchlist_pool.acquire() as wconn:
        await wconn.execute("""
            CREATE TABLE IF NOT EXISTS watchlist (
              id SERIAL PRIMARY KEY,
              user_id TEXT NOT NULL,
              card_id BIGINT NOT NULL,
              player_name TEXT NOT NULL,
              version TEXT,
              platform TEXT NOT NULL,         -- 'ps' | 'xbox'
              started_price INTEGER NOT NULL,
              started_at TIMESTAMP NOT NULL DEFAULT NOW(),
              last_price INTEGER,
              last_checked TIMESTAMP,
              notes TEXT
            )
        """)
        await wconn.execute("CREATE INDEX IF NOT EXISTS idx_watchlist_user ON watchlist(user_id)")
        await wconn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_watchlist_unique
            ON watchlist(user_id, card_id, platform)
        """)

    try:
        yield
    finally:
        if pool: await pool.close()
        if player_pool: await player_pool.close()
        if watchlist_pool: await watchlist_pool.close()

# --------- APP (create BEFORE routes) ---------
app = FastAPI(lifespan=lifespan)

# --------- MIDDLEWARE ---------
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    same_site="none",
    https_only=True,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        FRONTEND_URL,
        "http://localhost:5173",
        "http://localhost:3000",
    ],
    allow_origin_regex=r"https://.*\.railway\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --------- DEPENDENCIES & HELPERS ---------
async def get_db():
    async with pool.acquire() as connection:
        yield connection

async def get_watchlist_db():
    async with watchlist_pool.acquire() as connection:
        yield connection

def get_current_user(request: Request) -> str:
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user_id

async def get_discord_user_info(access_token: str):
    async with aiohttp.ClientSession() as session:
        async with session.get(DISCORD_USERS_ME, headers={"Authorization": f"Bearer {access_token}"}) as resp:
            if resp.status != 200:
                return None
            return await resp.json()

async def check_server_membership(user_id: str) -> bool:
    if not (DISCORD_BOT_TOKEN and DISCORD_SERVER_ID):
        return True
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://discord.com/api/guilds/{DISCORD_SERVER_ID}/members/{user_id}",
                headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
            ) as resp:
                return resp.status == 200
    except Exception:
        return False

def issue_extension_token(discord_id: str) -> str:
    now = int(time.time())
    payload = {"sub": discord_id, "scope": "trade:ingest", "iat": now, "exp": now + JWT_TTL_SECONDS, "iss": JWT_ISSUER}
    return jwt.encode(payload, JWT_PRIVATE_KEY, algorithm="HS256")

def require_extension_jwt(request: Request):
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = auth[7:]
    try:
        payload = jwt.decode(token, JWT_PRIVATE_KEY, algorithms=["HS256"], issuer=JWT_ISSUER)
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")
    return SimpleNamespace(discord_id=payload.get("sub"))

# --------- FUT.GG price fetch (watchlist helper) ---------
async def fetch_price(card_id: int, platform: str) -> Dict[str, Any]:
    """
    platform: 'ps' or 'xbox'
    Returns: {"price": int|None, "isExtinct": bool, "updatedAt": str|None}
    """
    platform = (platform or "").lower()
    key = f"{card_id}|{platform}"
    now = time.time()

    # short TTL cache
    if key in _price_cache and (now - _price_cache[key]["at"] < PRICE_CACHE_TTL):
        c = _price_cache[key]
        return {"price": c["price"], "isExtinct": c["isExtinct"], "updatedAt": c["updatedAt"]}

    url = f"{FUTGG_BASE}/{card_id}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-GB,en;q=0.9",
        "Referer": "https://www.fut.gg/",
        "Origin": "https://www.fut.gg",
    }

    async with aiohttp.ClientSession() as sess:
        async with sess.get(url, headers=headers) as r:
            if r.status != 200:
                cached = _price_cache.get(key)
                if cached:
                    return {"price": cached["price"], "isExtinct": cached["isExtinct"], "updatedAt": cached["updatedAt"]}
                raise HTTPException(status_code=502, detail="Failed to fetch price")
            data = await r.json()

    current = (data.get("data") or {}).get("currentPrice") or {}
    price = current.get("price")
    is_extinct = current.get("isExtinct", False)
    updated_at = current.get("priceUpdatedAt")

    _price_cache[key] = {"at": now, "price": price, "isExtinct": is_extinct, "updatedAt": updated_at}
    return {"price": price, "isExtinct": is_extinct, "updatedAt": updated_at}

# --------- ROUTES ---------
@app.get("/")
async def root():
    return {"message": "FUT Dashboard API", "status": "healthy"}

@app.get("/health")
async def health_check():
    try:
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return {"status": "healthy", "database": "connected"}
    except Exception as e:
        return {"status": "unhealthy", "database": "disconnected", "error": str(e)}

# OAuth – dashboard
@app.get("/api/login")
async def login():
    return RedirectResponse(
        f"https://discord.com/oauth2/authorize?client_id={DISCORD_CLIENT_ID}&redirect_uri={DISCORD_REDIRECT_URI}&response_type=code&scope=identify"
    )

@app.get("/api/callback")
async def callback(request: Request):
    code  = request.query_params.get("code")
    state = request.query_params.get("state")
    if not code:
        raise HTTPException(status_code=400, detail="Missing code")

    token_url = "https://discord.com/api/oauth2/token"
    data = {
        "client_id": DISCORD_CLIENT_ID,
        "client_secret": DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": DISCORD_REDIRECT_URI,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(token_url, data=data, headers=headers) as resp:
                if resp.status != 200:
                    raise HTTPException(status_code=400, detail="OAuth token exchange failed")
                token_data = await resp.json()
                access_token = token_data.get("access_token")
                if not access_token:
                    raise HTTPException(status_code=400, detail="OAuth failed")

        user_data = await get_discord_user_info(access_token)
        if not user_data:
            raise HTTPException(status_code=400, detail="Failed to fetch user data")

        user_id = user_data["id"]

        # Extension flow
        if state and state in OAUTH_STATE:
            meta = OAUTH_STATE.pop(state)
            jwt_token = issue_extension_token(user_id)
            ext_redirect = meta["ext_redirect"]
            return RedirectResponse(f"{ext_redirect}#token={jwt_token}&state={state}")

        # Site login flow
        is_member = await check_server_membership(user_id)
        if not is_member:
            return RedirectResponse(f"{FRONTEND_URL}/access-denied")

        username = f"{user_data['username']}#{user_data.get('discriminator', '0000')}"
        avatar_url = (
            f"https://cdn.discordapp.com/avatars/{user_id}/{user_data['avatar']}.png"
            if user_data.get('avatar')
            else f"https://cdn.discordapp.com/embed/avatars/{int(user_data.get('discriminator', '0')) % 5}.png"
        )
        global_name = user_data.get('global_name') or user_data['username']

        request.session["user_id"] = user_id
        request.session["username"] = username
        request.session["avatar_url"] = avatar_url
        request.session["global_name"] = global_name

        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO portfolio (user_id, starting_balance) VALUES ($1, $2) "
                "ON CONFLICT (user_id) DO NOTHING",
                user_id, 0
            )
            await conn.execute(
                """
                INSERT INTO user_profiles (user_id, username, avatar_url, global_name, updated_at)
                VALUES ($1, $2, $3, $4, NOW())
                ON CONFLICT (user_id)
                DO UPDATE SET username = $2, avatar_url = $3, global_name = $4, updated_at = NOW()
                """,
                user_id, username, avatar_url, global_name
            )

        return RedirectResponse(f"{FRONTEND_URL}/?authenticated=true")
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"OAuth error: {e}")
        raise HTTPException(status_code=500, detail="Authentication failed")

@app.get("/api/logout")
async def logout(request: Request):
    request.session.clear()
    return {"message": "Logged out successfully"}

# OAuth – Chrome extension
@app.get("/oauth/start")
async def oauth_start(redirect_uri: str):
    if not redirect_uri.startswith("https://") or "chromiumapp.org" not in redirect_uri:
        raise HTTPException(400, "Invalid redirect_uri")
    state = secrets.token_urlsafe(24)
    OAUTH_STATE[state] = {"ext_redirect": redirect_uri, "ts": time.time()}
    params = {
        "client_id": DISCORD_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": DISCORD_REDIRECT_URI,
        "scope": "identify",
        "state": state,
        "prompt": "consent",
    }
    return RedirectResponse(f"{DISCORD_OAUTH_AUTHORIZE}?{urlencode(params)}")

# --- Business routes (dashboard, trades, settings, goals, analytics, bulk, export/import, search) ---

# Dashboard helper
async def fetch_dashboard_data(user_id: str, conn):
    portfolio = await conn.fetchrow("SELECT starting_balance FROM portfolio WHERE user_id=$1", user_id)
    stats = await conn.fetchrow(
        "SELECT COALESCE(SUM(profit),0) AS total_profit, COALESCE(SUM(ea_tax),0) AS total_tax, COUNT(*) as total_trades FROM trades WHERE user_id=$1",
        user_id,
    )
    trades = await conn.fetch(
        "SELECT player, version, buy, sell, quantity, platform, profit, ea_tax, tag, timestamp "
        "FROM trades WHERE user_id=$1 ORDER BY timestamp DESC LIMIT 10",
        user_id,
    )
    all_trades = await conn.fetch("SELECT profit FROM trades WHERE user_id=$1 ORDER BY timestamp DESC", user_id)
    win_count = len([t for t in all_trades if t["profit"] and t["profit"] > 0])
    win_rate = round((win_count / len(all_trades)) * 100, 1) if all_trades else 0
    tag_stats = await conn.fetch("SELECT tag, COUNT(*) as count FROM trades WHERE user_id=$1 GROUP BY tag ORDER BY count DESC LIMIT 1", user_id)
    most_used_tag = tag_stats[0]["tag"] if tag_stats else "N/A"
    best_trade = await conn.fetchrow("SELECT * FROM trades WHERE user_id=$1 ORDER BY profit DESC LIMIT 1", user_id)
    return {
        "netProfit": stats["total_profit"] or 0,
        "taxPaid": stats["total_tax"] or 0,
        "startingBalance": portfolio["starting_balance"] if portfolio else 0,
        "trades": [dict(row) for row in trades],
        "profile": {
            "totalProfit": stats["total_profit"] or 0,
            "tradesLogged": stats["total_trades"] or 0,
            "winRate": win_rate,
            "mostUsedTag": most_used_tag,
            "bestTrade": dict(best_trade) if best_trade else None,
        }
    }

@app.get("/api/dashboard")
async def get_dashboard(user_id: str = Depends(get_current_user), conn = Depends(get_db)):
    return await fetch_dashboard_data(user_id, conn)

@app.get("/api/profile")
async def get_profile(user_id: str = Depends(get_current_user), conn = Depends(get_db)):
    return await fetch_dashboard_data(user_id, conn)

@app.post("/api/trades")
async def add_trade(request: Request, user_id: str = Depends(get_current_user), conn = Depends(get_db)):
    data = await request.json()
    required_fields = ["player", "version", "buy", "sell", "quantity", "platform", "tag"]
    missing_fields = [f for f in required_fields if f not in data or data[f] == ""]
    if missing_fields:
        raise HTTPException(status_code=400, detail=f"Missing required fields: {missing_fields}")
    try:
        quantity = int(data["quantity"]); buy = int(data["buy"]); sell = int(data["sell"])
        if quantity <= 0 or buy <= 0 or sell <= 0:
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid numeric values")
    profit = (sell - buy) * quantity
    ea_tax = int(sell * quantity * 0.05)
    await conn.execute(
        """INSERT INTO trades (user_id, player, version, buy, sell, quantity, platform, profit, ea_tax, tag, timestamp)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,NOW())""",
        user_id, data["player"], data["version"], buy, sell, quantity, data["platform"], profit, ea_tax, data["tag"]
    )
    return {"message": "Trade added successfully!", "profit": profit, "ea_tax": ea_tax}

@app.get("/api/trades")
async def get_all_trades(user_id: str = Depends(get_current_user), conn = Depends(get_db)):
    rows = await conn.fetch("SELECT * FROM trades WHERE user_id=$1 ORDER BY timestamp DESC", user_id)
    return {"trades": [dict(r) for r in rows]}

@app.delete("/api/trades/{trade_id}")
async def delete_trade(trade_id: int, user_id: str = Depends(get_current_user), conn = Depends(get_db)):
    result = await conn.execute("DELETE FROM trades WHERE id=$1 AND user_id=$2", trade_id, user_id)
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Trade not found")
    return {"message": "Trade deleted successfully"}

# FUT.GG proxies
@app.get("/api/fut-player-definition/{card_id}")
async def get_player_definition(card_id: str):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://www.fut.gg/api/fut/player-item-definitions/25/{card_id}/",
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "en-GB,en;q=0.9",
                    "Referer": "https://www.fut.gg/",
                    "Origin": "https://www.fut.gg"
                }
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                return {"error": f"API returned status {resp.status}"}
    except Exception as e:
        logging.error(f"Player definition fetch error: {e}")
        return {"error": str(e)}

@app.get("/api/fut-player-price/{card_id}")
async def get_player_price(card_id: str):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://www.fut.gg/api/fut/player-prices/25/{card_id}",
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "en-GB,en;q=0.9",
                    "Referer": "https://www.fut.gg/",
                    "Origin": "https://www.fut.gg"
                }
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                return {"error": f"API returned status {resp.status}"}
    except Exception as e:
        logging.error(f"Player price fetch error: {e}")
        return {"error": str(e)}

# --------- WATCHLIST ROUTES ---------
@app.post("/api/watchlist")
async def add_watch_item(payload: WatchlistCreate, user_id: str = Depends(get_current_user), conn = Depends(get_watchlist_db)):
    live = await fetch_price(payload.card_id, payload.platform)
    start_price = live["price"] if isinstance(live["price"], int) else 0

    row = await conn.fetchrow(
        """
        INSERT INTO watchlist (user_id, card_id, player_name, version, platform, started_price, last_price, last_checked, notes)
        VALUES ($1,$2,$3,$4,$5,$6,$7,NOW(),$8)
        ON CONFLICT (user_id, card_id, platform) DO UPDATE
          SET player_name=EXCLUDED.player_name,
              version=EXCLUDED.version,
              notes=EXCLUDED.notes,
              last_price=EXCLUDED.last_price,
              last_checked=NOW()
        RETURNING id
        """,
        user_id,
        payload.card_id,
        payload.player_name,
        payload.version,
        payload.platform.lower(),
        start_price,
        live["price"] if isinstance(live["price"], int) else None,
        payload.notes,
    )
    return {"ok": True, "id": row["id"], "start_price": start_price, "is_extinct": live.get("isExtinct", False)}

@app.get("/api/watchlist")
async def list_watch_items(user_id: str = Depends(get_current_user), conn = Depends(get_watchlist_db)):
    rows = await conn.fetch("SELECT * FROM watchlist WHERE user_id=$1 ORDER BY started_at DESC", user_id)
    watches = [dict(r) for r in rows]
    if not watches:
        return {"ok": True, "items": []}

    card_ids = [w["card_id"] for w in watches]
    async with player_pool.acquire() as pconn:
        meta_rows = await pconn.fetch(
            "SELECT card_id, name, rating, club, nation FROM fut_players WHERE card_id = ANY($1)",
            card_ids
        )
    meta_map = {int(m["card_id"]): {k: m[k] for k in ("name", "rating", "club", "nation")} for m in meta_rows}

    enriched = []
    for w in watches:
        live = await fetch_price(w["card_id"], w["platform"])
        live_price = live.get("price")
        change = change_pct = None
        if (isinstance(live_price, int) or isinstance(live_price, float)) and (w["started_price"] and w["started_price"] > 0):
            change = int(live_price) - int(w["started_price"])
            change_pct = round((change / int(w["started_price"])) * 100, 2)

        m = meta_map.get(int(w["card_id"]), {})
        enriched.append({
            "id": w["id"],
            "card_id": w["card_id"],
            "player_name": w["player_name"],
            "version": w["version"],
            "platform": w["platform"],
            "started_price": w["started_price"],
            "started_at": w["started_at"].isoformat(),
            "current_price": live_price if isinstance(live_price, int) else None,
            "is_extinct": live.get("isExtinct", False),
            "updated_at": live.get("updatedAt"),
            "change": change,
            "change_pct": change_pct,
            "notes": w["notes"],
            "name": m.get("name"),
            "rating": m.get("rating"),
            "club": m.get("club"),
            "nation": m.get("nation"),
        })
    return {"ok": True, "items": enriched}

@app.delete("/api/watchlist/{watch_id}")
async def delete_watch_item(watch_id: int, user_id: str = Depends(get_current_user), conn = Depends(get_watchlist_db)):
    res = await conn.execute("DELETE FROM watchlist WHERE id=$1 AND user_id=$2", watch_id, user_id)
    if res == "DELETE 0":
        raise HTTPException(status_code=404, detail="Watch item not found")
    return {"ok": True}

@app.post("/api/watchlist/{watch_id}/refresh")
async def refresh_watch_item(watch_id: int, user_id: str = Depends(get_current_user), conn = Depends(get_watchlist_db)):
    w = await conn.fetchrow("SELECT * FROM watchlist WHERE id=$1 AND user_id=$2", watch_id, user_id)
    if not w:
        raise HTTPException(status_code=404, detail="Watch item not found")

    live = await fetch_price(w["card_id"], w["platform"])
    live_price = live.get("price") if isinstance(live.get("price"), int) else None

    await conn.execute("UPDATE watchlist SET last_price=$1, last_checked=NOW() WHERE id=$2", live_price, watch_id)

    change = change_pct = None
    if live_price is not None and w["started_price"] > 0:
        change = int(live_price) - int(w["started_price"])
        change_pct = round((change / int(w["started_price"])) * 100, 2)

    async with player_pool.acquire() as pconn:
        meta = await pconn.fetchrow(
            "SELECT card_id, name, rating, club, nation FROM fut_players WHERE card_id=$1",
            w["card_id"]
        )
    meta_dict = dict(meta) if meta else {}

    return {
        "ok": True,
        "item": {
            "id": w["id"],
            "card_id": w["card_id"],
            "player_name": w["player_name"],
            "version": w["version"],
            "platform": w["platform"],
            "started_price": w["started_price"],
            "started_at": w["started_at"].isoformat(),
            "current_price": live_price,
            "is_extinct": live.get("isExtinct", False),
            "updated_at": live.get("updatedAt"),
            "change": change,
            "change_pct": change_pct,
            "notes": w["notes"],
            "name": meta_dict.get("name"),
            "rating": meta_dict.get("rating"),
            "club": meta_dict.get("club"),
            "nation": meta_dict.get("nation"),
        }
    }

# Me
@app.get("/api/me")
async def get_current_user_info(request: Request, user_id: str = Depends(get_current_user)):
    return {
        "user_id": user_id,
        "username": request.session.get("username"),
        "avatar_url": request.session.get("avatar_url"),
        "global_name": request.session.get("global_name"),
        "authenticated": True
    }

# Settings
@app.get("/api/settings")
async def get_user_settings(user_id: str = Depends(get_current_user), conn = Depends(get_db)):
    settings = await conn.fetchrow("SELECT settings FROM user_settings WHERE user_id=$1", user_id)
    return settings["settings"] if settings else UserSettings().dict()

@app.post("/api/settings")
async def update_user_settings(settings: UserSettings, user_id: str = Depends(get_current_user), conn = Depends(get_db)):
    await conn.execute(
        """INSERT INTO user_settings (user_id, settings, updated_at)
           VALUES ($1, $2, NOW())
           ON CONFLICT (user_id) DO UPDATE SET settings = $2, updated_at = NOW()""",
        user_id, json.dumps(settings.dict())
    )
    return {"message": "Settings updated successfully"}

@app.post("/api/portfolio/balance")
async def update_starting_balance(request: Request, user_id: str = Depends(get_current_user), conn = Depends(get_db)):
    data = await request.json()
    starting_balance = int(data.get("starting_balance", 0))
    await conn.execute(
        "INSERT INTO portfolio (user_id, starting_balance) VALUES ($1, $2) "
        "ON CONFLICT (user_id) DO UPDATE SET starting_balance = $2",
        user_id, starting_balance
    )
    return {"message": "Starting balance updated successfully"}

# Goals
@app.get("/api/goals")
async def get_trading_goals(user_id: str = Depends(get_current_user), conn = Depends(get_db)):
    goals = await conn.fetch("SELECT * FROM trading_goals WHERE user_id=$1 ORDER BY created_at DESC", user_id)
    return {"goals": [dict(g) for g in goals]}

@app.post("/api/goals")
async def create_trading_goal(goal: TradingGoal, user_id: str = Depends(get_current_user), conn = Depends(get_db)):
    await conn.execute(
        """INSERT INTO trading_goals (user_id, title, target_amount, target_date, goal_type, is_completed, created_at)
           VALUES ($1, $2, $3, $4, $5, $6, NOW())""",
        user_id, goal.title, goal.target_amount, goal.target_date, goal.goal_type, goal.is_completed
    )
    return {"message": "Goal created successfully"}

# Analytics
@app.get("/api/analytics/advanced")
async def get_advanced_analytics(user_id: str = Depends(get_current_user), conn = Depends(get_db)):
    daily_profits = await conn.fetch("""
        SELECT DATE(timestamp) as date, COALESCE(SUM(profit), 0) as daily_profit, COUNT(*) as trades_count
        FROM trades WHERE user_id=$1 AND timestamp >= NOW() - INTERVAL '30 days'
        GROUP BY DATE(timestamp) ORDER BY date
    """, user_id)
    tag_performance = await conn.fetch("""
        SELECT tag, COUNT(*) as trade_count, COALESCE(SUM(profit), 0) as total_profit,
               COALESCE(AVG(profit), 0) as avg_profit,
               COUNT(CASE WHEN profit > 0 THEN 1 END) * 100.0 / COUNT(*) as win_rate
        FROM trades WHERE user_id=$1 AND tag IS NOT NULL AND tag != ''
        GROUP BY tag ORDER BY total_profit DESC
    """, user_id)
    platform_stats = await conn.fetch("""
        SELECT platform, COUNT(*) as trade_count, COALESCE(SUM(profit), 0) as total_profit,
               COALESCE(AVG(profit), 0) as avg_profit
        FROM trades WHERE user_id=$1 GROUP BY platform
    """, user_id)
    monthly_summary = await conn.fetch("""
        SELECT DATE_TRUNC('month', timestamp) as month, COUNT(*) as trades_count,
               COALESCE(SUM(profit), 0) as total_profit, COALESCE(SUM(ea_tax), 0) as total_tax
        FROM trades WHERE user_id=$1
        GROUP BY DATE_TRUNC('month', timestamp) ORDER BY month DESC LIMIT 12
    """, user_id)
    return {
        "daily_profits": [dict(r) for r in daily_profits],
        "tag_performance": [dict(r) for r in tag_performance],
        "platform_stats": [dict(r) for r in platform_stats],
        "monthly_summary": [dict(r) for r in monthly_summary],
    }

# Bulk update
@app.put("/api/trades/bulk")
async def bulk_edit_trades(request: Request, user_id: str = Depends(get_current_user), conn = Depends(get_db)):
    data = await request.json()
    trade_ids = data.get('trade_ids', [])
    updates = data.get('updates', {})
    if not trade_ids or not updates:
        raise HTTPException(status_code=400, detail="trade_ids and updates required")
    set_clauses, params = [], []
    if 'tag' in updates:
        set_clauses.append(f"tag = ${len(params)+1}"); params.append(updates['tag'])
    if 'platform' in updates:
        set_clauses.append(f"platform = ${len(params)+1}"); params.append(updates['platform'])
    if not set_clauses:
        raise HTTPException(status_code=400, detail="No valid updates provided")
    params.extend([user_id, trade_ids])
    query = f"""UPDATE trades SET {', '.join(set_clauses)} WHERE user_id = ${len(params)-1} AND id = ANY(${len(params)})"""
    await conn.execute(query, *params)
    return {"message": f"Updated {len(trade_ids)} trades successfully"}

# Export
@app.get("/api/export/trades")
async def export_trades(format: str = "csv", user_id: str = Depends(get_current_user), conn = Depends(get_db)):
    rows = await conn.fetch("SELECT * FROM trades WHERE user_id=$1 ORDER BY timestamp DESC", user_id)
    data = [dict(r) for r in rows]
    if format.lower() == "json":
        blob = json.dumps(data, indent=2, default=str).encode()
        return StreamingResponse(io.BytesIO(blob), media_type="application/json",
                                 headers={"Content-Disposition": "attachment; filename=trades_export.json"})
    output = io.StringIO()
    if data:
        writer = csv.DictWriter(output, fieldnames=data[0].keys())
        writer.writeheader()
        writer.writerows(data)
    return StreamingResponse(io.StringIO(output.getvalue()), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=trades_export.csv"})

# Import
@app.post("/api/import/trades")
async def import_trades(file: UploadFile = File(...), user_id: str = Depends(get_current_user), conn = Depends(get_db)):
    contents = await file.read()
    if file.filename.endswith('.json'):
        payload = json.loads(contents.decode('utf-8'))
        trades_to_import = payload if isinstance(payload, list) else [payload]
    elif file.filename.endswith('.csv'):
        reader = csv.DictReader(io.StringIO(contents.decode('utf-8')))
        trades_to_import = list(reader)
    else:
        raise HTTPException(status_code=400, detail="Unsupported file format")

    imported_count, errors = 0, []
    for t in trades_to_import:
        try:
            player = t.get('player', '').strip()
            version = t.get('version', '').strip()
            buy = int(t.get('buy', 0)); sell = int(t.get('sell', 0))
            quantity = int(t.get('quantity', 1))
            platform = t.get('platform', 'Console').strip()
            tag = t.get('tag', '').strip()
            if not player or not version or buy <= 0 or sell <= 0:
                errors.append(f"Invalid trade data: {t}"); continue
            profit = (sell - buy) * quantity
            ea_tax = int(sell * quantity * 0.05)
            await conn.execute(
                """INSERT INTO trades (user_id, player, version, buy, sell, quantity, platform, profit, ea_tax, tag, timestamp)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,NOW())""",
                user_id, player, version, buy, sell, quantity, platform, profit, ea_tax, tag
            )
            imported_count += 1
        except Exception as e:
            errors.append(f"Error importing trade {t}: {str(e)}")
    return {"message": f"Successfully imported {imported_count} trades", "imported_count": imported_count, "errors": errors[:10]}

# Delete all
@app.delete("/api/data/delete-all")
async def delete_all_user_data(confirm: bool = False, user_id: str = Depends(get_current_user), conn = Depends(get_db)):
    if not confirm:
        raise HTTPException(status_code=400, detail="Confirmation required")
    await conn.execute("DELETE FROM trades WHERE user_id=$1", user_id)
    await conn.execute("UPDATE portfolio SET starting_balance = 0 WHERE user_id=$1", user_id)
    return {"message": "All data deleted successfully", "trades_deleted": 0}

# Search players  ✅ (updated for your columns; also supports card_id search)
@app.get("/api/search-players")
async def search_players(q: str = ""):
    q = (q or "").strip()
    if not q:
        return {"players": []}
    try:
        async with player_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                  card_id,
                  name,
                  rating,
                  version,
                  image_url,
                  club,
                  nation,
                  position,
                  price
                FROM fut_players
                WHERE
                  LOWER(name) LIKE LOWER($1)        -- by name
                  OR card_id::text LIKE $1          -- or by id
                ORDER BY rating DESC NULLS LAST, name ASC
                LIMIT 20
                """,
                f"%{q}%"
            )
        players = [
            {
                "card_id": int(r["card_id"]),
                "name": r["name"],
                "rating": r["rating"],
                "version": r["version"],
                "image_url": r["image_url"],
                "club": r["club"],
                "nation": r["nation"],
                "position": r["position"],
                "price": r["price"],
            }
            for r in rows
        ]
        return {"players": players}
    except Exception as e:
        logging.error(f"Player search error: {e}")
        return {"players": [], "error": str(e)}

# Extension ingest
@app.post("/api/trades/ingest")
async def ingest_sale(sale: ExtSale, auth = Depends(require_extension_jwt)):
    ts = sale.timestamp_ms / 1000.0
    user_id = auth.discord_id
    player = (sale.player_name or "").strip()
    version = (sale.card_version or "").strip()
    buy = int(sale.buy_price or 0)
    sell = int(sale.sell_price)
    qty = 1
    platform = "Console"
    tag = "Auto"
    profit = (sell - buy) * qty
    ea_tax = int(round(sell * qty * 0.05))

    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                inserted = await conn.fetchrow("""
                    INSERT INTO fut_trades
                      (discord_id, trade_id, player_name, card_version, buy_price, sell_price, ts, source)
                    VALUES ($1,$2,$3,$4,$5,$6,to_timestamp($7),'webapp')
                    ON CONFLICT (discord_id, trade_id) DO NOTHING
                    RETURNING id
                """, user_id, sale.trade_id, player, (version or None), buy, sell, ts)

                if inserted:
                    await conn.execute("""
                        INSERT INTO trades
                          (user_id, player, version, buy, sell, quantity, platform, tag, notes, ea_tax, profit, timestamp, trade_id)
                        VALUES
                          ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, to_timestamp($12), $13)
                        ON CONFLICT (user_id, trade_id) DO NOTHING
                    """, user_id, player, version, buy, sell, qty, platform, tag, "", ea_tax, profit, ts, sale.trade_id)
        return {"ok": True, "mirrored_to_trades": bool(inserted)}
    except Exception as e:
        logging.error(f"Ingest error: {e}")
        raise HTTPException(status_code=500, detail="Failed to ingest sale")

# ---- entrypoint ----
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT)
