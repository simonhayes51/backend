import os
import re
import io
import csv
import jwt
import time
import json
import asyncio
import logging
import secrets
import aiohttp
import asyncpg

from bs4 import BeautifulSoup
from types import SimpleNamespace
from urllib.parse import urlencode
from contextlib import asynccontextmanager, suppress
from typing import Any, Dict, List, Literal, Optional, Tuple

from dotenv import load_dotenv
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta, timezone, time as dt_time

from fastapi import (
    FastAPI, APIRouter, Request, HTTPException, Depends,
    UploadFile, File, Query
)
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel

from app.services.price_history import get_price_history
from app.services.prices import get_player_price

# ✅ Trade Finder router
from app.routers.trade_finder import router as trade_finder_router

# ✅ Smart Buy router
from app.routers.smart_buy import router as smart_buy_router


# ----------------- BOOTSTRAP -----------------
logging.basicConfig(level=logging.INFO)
load_dotenv()

# --------- ENV ---------
required_env_vars = ["DATABASE_URL", "DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET", "DISCORD_REDIRECT_URI"]
missing_vars = [var for var in required_env_vars if not os.getenv(var)]
if missing_vars:
    raise ValueError(f"Missing required environment variables: {missing_vars}")

DATABASE_URL = os.getenv("DATABASE_URL")
PLAYER_DATABASE_URL = os.getenv("PLAYER_DATABASE_URL", DATABASE_URL)
WATCHLIST_DATABASE_URL = os.getenv("WATCHLIST_DATABASE_URL", DATABASE_URL)

DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI")
SECRET_KEY = os.getenv("SECRET_KEY")
FRONTEND_URL = (os.getenv("FRONTEND_URL", "https://app.futhub.co.uk").rstrip("/"))
PORT = int(os.getenv("PORT", 8000))

ENV = os.getenv("ENV", "production").lower()
IS_PROD = ENV in ("prod", "production")

# JWT / Discord
JWT_PRIVATE_KEY = os.getenv("JWT_PRIVATE_KEY", "dev-secret-change-me")
JWT_ISSUER = os.getenv("JWT_ISSUER", "fut-dashboard")
JWT_TTL_SECONDS = int(os.getenv("JWT_TTL_SECONDS", "2592000"))  # 30 days
DISCORD_OAUTH_AUTHORIZE = "https://discord.com/api/oauth2/authorize"
DISCORD_OAUTH_TOKEN = "https://discord.com/api/oauth2/token"
DISCORD_USERS_ME = "https://discord.com/api/users/@me"
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_SERVER_ID = os.getenv("DISCORD_SERVER_ID")

# Watchlist alert env
WATCHLIST_FALLBACK_CHANNEL_ID = os.getenv("WATCHLIST_FALLBACK_CHANNEL_ID")
WATCHLIST_POLL_INTERVAL = int(os.getenv("WATCHLIST_POLL_INTERVAL", "60"))  # seconds

# ephemeral state store
OAUTH_STATE: Dict[str, Dict[str, Any]] = {}

if not SECRET_KEY:
    raise ValueError("SECRET_KEY environment variable is required")

# --------- FUT.GG / Watchlist config ---------
FUTGG_BASE = "https://www.fut.gg/api/fut/player-prices/25"
PRICE_CACHE_TTL = 5  # seconds
_price_cache: Dict[str, Dict[str, Any]] = {}

# ----------------- FUT.GG MOMENTUM (NEW) -----------------
MOMENTUM_BASE = "https://www.fut.gg/players/momentum"
MOMENTUM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Referer": "https://www.fut.gg/",
}
_CARD_HREF_RE = re.compile(r"/players/(\d+)-[a-z0-9-]+/25-(\d+)/?", re.IGNORECASE)

def _norm_tf(tf: Optional[str]) -> str:
    if not tf:
        return "24"
    tf = tf.lower().strip()
    if tf.endswith("h"):
        tf = tf[:-1]
    return tf if tf in ("6", "12", "24") else "24"

async def _fetch_momentum_page(tf: str, page: int) -> str:
    url = f"{MOMENTUM_BASE}/{tf}/?page={page}"
    timeout = aiohttp.ClientTimeout(total=12)
    async with aiohttp.ClientSession(timeout=timeout, headers=MOMENTUM_HEADERS) as sess:
        async with sess.get(url) as r:
            if r.status != 200:
                raise HTTPException(status_code=502, detail=f"MOMENTUM {r.status}")
            return await r.text()

def _parse_last_page_number(html: str) -> int:
    soup = BeautifulSoup(html, "html.parser")
    nums = []
    for a in soup.find_all("a"):
        href = a.get("href") or ""
        if "page=" in href:
            try:
                n = int(href.split("page=", 1)[1].split("&", 1)[0])
                nums.append(n)
            except Exception:
                continue
        else:
            t = a.text.strip()
            if t.isdigit():
                nums.append(int(t))
    return max(nums) if nums else 1

def _extract_items(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    tiles = []
    for a in soup.find_all("a", href=True):
        m = _CARD_HREF_RE.search(a["href"])
        if not m:
            continue
        node = a
        for _ in range(4):
            if node is None or node.name == "body":
                break
            txt = node.get_text(separator=" ", strip=True)
            if "%" in txt:
                tiles.append((m.group(2), txt))
                break
            node = node.parent

    items = []
    pct_re = re.compile(r"([+\-]?\s?\d+(?:\.\d+)?)\s*%")
    seen = set()
    for cid, text in tiles:
        if cid in seen:
            continue
        m = pct_re.search(text)
        if not m:
            continue
        try:
            pct = float(m.group(1).replace(" ", ""))
            items.append({"card_id": int(cid), "percent": pct})
            seen.add(cid)
        except Exception:
            continue
    return items

async def _momentum_page_items(tf: str, page: int) -> tuple[list[dict], str]:
    html = await _fetch_momentum_page(tf, page)
    return _extract_items(html), html


# ----------------- HELPERS -----------------
def parse_coin_amount(v) -> int:
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        return int(round(float(v)))
    s = str(v).strip().lower()
    s = re.sub(r"[\s_]", "", s)
    s = re.sub(r"(?<=\d)[,\.](?=\d{3}\b)", "", s)
    s = s.replace(",", ".")
    if s.endswith("kk"):
        try: 
            return int(round(float(s[:-2]) * 1_000_000))
        except: 
            return 0
    if s.endswith("k"):
        try: 
            return int(round(float(s[:-1]) * 1_000))
        except: 
            return 0
    if s.endswith("m"):
        try: 
            return int(round(float(s[:-1]) * 1_000_000))
        except: 
            return 0
    try:
        return int(round(float(s)))
    except:
        return 0

# Time helpers
LONDON = ZoneInfo("Europe/London")
UTC = timezone.utc

def now_utc() -> datetime:
    return datetime.now(UTC)

def london_now() -> datetime:
    return datetime.now(LONDON)

def next_daily_london_hour(hour: int = 18) -> datetime:
    ln = london_now()
    tgt = ln.replace(hour=hour, minute=0, second=0, microsecond=0)
    if tgt <= ln:
        tgt = tgt + timedelta(days=1)
    return tgt.astimezone(UTC)

def is_within_quiet_hours(dt: datetime, quiet_start: Optional[dt_time], quiet_end: Optional[dt_time]) -> bool:
    if not quiet_start or not quiet_end:
        return False
    t = dt.astimezone(LONDON).time()
    if quiet_start <= quiet_end:
        return quiet_start <= t < quiet_end
    return t >= quiet_start or t < quiet_end


# ----------------- MODELS -----------------
class UserSettings(BaseModel):
    default_platform: Optional[str] = "Console"
    custom_tags: Optional[List[str]] = []
    currency_format: Optional[str] = "coins"
    theme: Optional[str] = "dark"
    timezone: Optional[str] = "UTC"
    date_format: Optional[str] = "US"
    include_tax_in_profit: Optional[bool] = True
    default_chart_range: Optional[str] = "30d"
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

class TradeUpdate(BaseModel):
    player: Optional[str] = None
    version: Optional[str] = None
    quantity: Optional[int] = None
    buy: Optional[Any] = None
    sell: Optional[Any] = None
    platform: Optional[str] = None
    tag: Optional[str] = None
    notes: Optional[str] = None
    timestamp: Optional[str] = None

class WatchlistCreate(BaseModel):
    card_id: int
    player_name: str
    version: Optional[str] = None
    platform: str  # "ps" | "xbox"
    notes: Optional[str] = None

# Alerts config
class WatchlistAlertCreate(BaseModel):
    card_id: int
    platform: str  # ps|xbox|pc
    rise_pct: Optional[float] = 5
    fall_pct: Optional[float] = 5
    ref_mode: Optional[str] = "last_close"  # last_close | fixed | started_price
    ref_price: Optional[float] = None
    cooloff_minutes: Optional[int] = 30
    quiet_start: Optional[str] = None  # "22:00"
    quiet_end: Optional[str] = None    # "07:00"
    prefer_dm: Optional[bool] = True
    fallback_channel_id: Optional[str] = None


# ----------------- POOLS & LIFESPAN -----------------
pool = None
player_pool = None
watchlist_pool = None
_watchlist_task: Optional[asyncio.Task] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool, player_pool, watchlist_pool, _watchlist_task

    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)

    if PLAYER_DATABASE_URL == DATABASE_URL:
        player_pool = pool
    else:
        player_pool = await asyncpg.create_pool(PLAYER_DATABASE_URL, min_size=1, max_size=10)

    if WATCHLIST_DATABASE_URL == DATABASE_URL:
        watchlist_pool = pool
    else:
        watchlist_pool = await asyncpg.create_pool(WATCHLIST_DATABASE_URL, min_size=1, max_size=10)

    # ✅ expose pools to routers/services that access request.app.state.*
    app.state.pool = pool
    app.state.player_pool = player_pool
    app.state.watchlist_pool = watchlist_pool

    # Core tables + indexes
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS usersettings (
                id SERIAL PRIMARY KEY,
                user_id VARCHAR(255) UNIQUE NOT NULL,
                default_platform VARCHAR(50) DEFAULT 'Console',
                custom_tags JSONB DEFAULT '[]',
                currency_format VARCHAR(20) DEFAULT 'coins',
                theme VARCHAR(20) DEFAULT 'dark',
                timezone VARCHAR(50) DEFAULT 'UTC',
                date_format VARCHAR(10) DEFAULT 'US',
                include_tax_in_profit BOOLEAN DEFAULT true,
                default_chart_range VARCHAR(10) DEFAULT '30d',
                visible_widgets JSONB DEFAULT '["profit", "tax", "balance", "trades"]',
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
        await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS trades_user_trade_uidx ON trades (user_id, trade_id)")
        await conn.execute("DROP INDEX IF EXISTS idx_trades_date")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_user_ts ON trades(user_id, timestamp)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_tag ON trades(user_id, tag)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_platform ON trades(user_id, platform)")
        
        # Backfill trade_id
        await conn.execute("""
            WITH to_fix AS (
              SELECT ctid, user_id,
                     ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY timestamp, player) AS rn
              FROM trades
              WHERE trade_id IS NULL
            )
            UPDATE trades t
               SET trade_id = ((EXTRACT(EPOCH FROM NOW())*1000)::bigint) + tf.rn
            FROM to_fix tf
            WHERE t.ctid = tf.ctid AND t.trade_id IS NULL
        """)
        
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
        await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS fut_trades_uidx ON fut_trades (discord_id, trade_id)")

        # Events table (for Next Promo)
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
          id BIGSERIAL PRIMARY KEY,
          name TEXT NOT NULL,
          kind TEXT NOT NULL,
          start_at TIMESTAMPTZ NOT NULL,
          end_at TIMESTAMPTZ,
          confidence TEXT NOT NULL DEFAULT 'heuristic',
          source TEXT NOT NULL DEFAULT 'rule:18:00',
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )""")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_events_start ON events(start_at)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind)")

        # ✅ Smart Buy tables
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS smart_buy_suggestions (
                id BIGSERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                card_id TEXT NOT NULL,
                suggestion_type VARCHAR(50) NOT NULL,
                current_price INTEGER NOT NULL,
                target_price INTEGER NOT NULL,
                expected_profit INTEGER NOT NULL,
                risk_level VARCHAR(20) NOT NULL,
                confidence_score INTEGER NOT NULL,
                priority_score INTEGER NOT NULL,
                reasoning TEXT NOT NULL,
                time_to_profit VARCHAR(50),
                platform VARCHAR(10) NOT NULL,
                market_state VARCHAR(30) NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                expires_at TIMESTAMP WITH TIME ZONE
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_smart_buy_suggestions_user_created ON smart_buy_suggestions(user_id, created_at DESC)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_smart_buy_suggestions_card_platform ON smart_buy_suggestions(card_id, platform)")
        
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS smart_buy_feedback (
                id BIGSERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                card_id TEXT NOT NULL,
                action VARCHAR(20) NOT NULL,
                notes TEXT,
                actual_buy_price INTEGER,
                actual_sell_price INTEGER,
                actual_profit INTEGER,
                timestamp TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_smart_buy_feedback_user_action ON smart_buy_feedback(user_id, action)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_smart_buy_feedback_card ON smart_buy_feedback(card_id, action)")
        
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS market_states (
                id BIGSERIAL PRIMARY KEY,
                platform VARCHAR(10) NOT NULL,
                state VARCHAR(30) NOT NULL,
                confidence_score INTEGER NOT NULL,
                detected_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                indicators JSONB
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_market_states_platform_detected ON market_states(platform, detected_at DESC)")

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS smart_buy_preferences (
                id BIGSERIAL PRIMARY KEY,
                user_id TEXT UNIQUE NOT NULL,
                default_budget INTEGER DEFAULT 100000,
                risk_tolerance VARCHAR(20) DEFAULT 'moderate',
                preferred_time_horizon VARCHAR(20) DEFAULT 'short',
                preferred_categories JSONB DEFAULT '[]'::jsonb,
                excluded_positions JSONB DEFAULT '[]'::jsonb,
                preferred_leagues JSONB DEFAULT '[]'::jsonb,
                preferred_nations JSONB DEFAULT '[]'::jsonb,
                min_rating INTEGER DEFAULT 75,
                max_rating INTEGER DEFAULT 95,
                min_profit INTEGER DEFAULT 1000,
                notifications_enabled BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_smart_buy_preferences_user ON smart_buy_preferences(user_id)")

    # Existing watchlist table on watchlist_pool
    async with watchlist_pool.acquire() as wconn:
        await wconn.execute("""
            CREATE TABLE IF NOT EXISTS watchlist (
              id SERIAL PRIMARY KEY,
              user_id TEXT NOT NULL,
              card_id BIGINT NOT NULL,
              player_name TEXT NOT NULL,
              version TEXT,
              platform TEXT NOT NULL,
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

        # Alerts config + log
        await wconn.execute("""
        CREATE TABLE IF NOT EXISTS watchlist_alerts (
          id BIGSERIAL PRIMARY KEY,
          user_id TEXT NOT NULL,
          user_discord_id TEXT,
          card_id BIGINT NOT NULL,
          platform TEXT NOT NULL CHECK (platform IN ('ps','xbox','pc')),
          ref_mode TEXT NOT NULL DEFAULT 'last_close', -- last_close | fixed | started_price
          ref_price NUMERIC,
          rise_pct NUMERIC DEFAULT 5,
          fall_pct NUMERIC DEFAULT 5,
          cooloff_minutes INT NOT NULL DEFAULT 30,
          quiet_start TIME,
          quiet_end TIME,
          prefer_dm BOOLEAN NOT NULL DEFAULT TRUE,
          fallback_channel_id TEXT,
          last_alert_at TIMESTAMPTZ,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )""")
        await wconn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_user ON watchlist_alerts(user_id)")
        await wconn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_pair ON watchlist_alerts(card_id, platform)")

        await wconn.execute("""
        CREATE TABLE IF NOT EXISTS alerts_log (
          id BIGSERIAL PRIMARY KEY,
          user_id TEXT NOT NULL,
          user_discord_id TEXT,
          card_id BIGINT NOT NULL,
          platform TEXT NOT NULL,
          direction TEXT NOT NULL,
          pct NUMERIC NOT NULL,
          price NUMERIC NOT NULL,
          ref_mode TEXT NOT NULL,
          ref_price NUMERIC,
          sent_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )""")
        await wconn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_user_time ON alerts_log(user_id, sent_at)")

    # Start alerts loop
    _watchlist_task = asyncio.create_task(_alerts_poll_loop())
    logging.info("✅ Watchlist alerts loop started (%ss)", WATCHLIST_POLL_INTERVAL)

    try:
        yield
    finally:
        if _watchlist_task:
            _watchlist_task.cancel()
            with suppress(asyncio.CancelledError):
                await _watchlist_task
        to_close = {pool, player_pool, watchlist_pool}
        for p in to_close:
            if p is not None:
                await p.close()


# ----------------- APP & MIDDLEWARE -----------------
app = FastAPI(lifespan=lifespan)

FRONTEND_URL = os.getenv("FRONTEND_ORIGIN", "https://app.futhub.co.uk")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        FRONTEND_URL,
        "https://app.futhub.co.uk",
        "https://www.futhub.co.uk",
        "https://futhub.co.uk",
        "http://localhost:5173",
        "http://localhost:3000",
    ],
    allow_origin_regex=r"^(https://.*\.railway\.app|chrome-extension://.*)$",
    allow_credentials=True,
    allow_methods=["GET","POST","PUT","PATCH","DELETE","OPTIONS"],
    allow_headers=["Authorization","Content-Type","X-Requested-With","Accept"],
    expose_headers=["Content-Disposition"],
    max_age=600,
)

app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    same_site="none" if IS_PROD else "lax",
    https_only=IS_PROD,
)

# ✅ mount Trade Finder API
app.include_router(trade_finder_router, prefix="/api")

# ✅ mount Smart Buy API
app.include_router(smart_buy_router, prefix="/api")

# ----------------- DEPENDENCIES & HELPERS -----------------
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

# --------- FUT.GG price fetch ---------
async def fetch_price(card_id: int, platform: str) -> Dict[str, Any]:
    platform = (platform or "").lower()
    key = f"{card_id}|{platform}"
    now = time.time()

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


# ----------------- EXTENSION ROUTER (no NameError) -----------------
ext_router = APIRouter()

@ext_router.get("/ext/ping")
async def ext_ping(auth = Depends(require_extension_jwt)):
    """Sanity check for the extension – proves the JWT is valid."""
    return {"ok": True, "sub": auth.discord_id}

@ext_router.post("/ext/trades")
async def ext_add_trade(
    sale: ExtSale,
    auth = Depends(require_extension_jwt),
    conn = Depends(get_db),
):
    """
    Ingest a sold item from the Chrome extension.
    The extension must send Authorization: Bearer <token> (issued during OAuth).
    """
    discord_id = auth.discord_id or "unknown"
    ts = datetime.fromtimestamp(int(sale.timestamp_ms) / 1000, tz=timezone.utc)

    # 1) Raw log into fut_trades (idempotent per discord_id + trade_id)
    await conn.execute("""
        INSERT INTO fut_trades (discord_id, trade_id, player_name, card_version, buy_price, sell_price, ts, source)
        VALUES ($1,$2,$3,$4,$5,$6,$7,'webapp')
        ON CONFLICT (discord_id, trade_id)
        DO UPDATE SET
            player_name  = EXCLUDED.player_name,
            card_version = COALESCE(EXCLUDED.card_version, fut_trades.card_version),
            buy_price    = COALESCE(EXCLUDED.buy_price, fut_trades.buy_price),
            sell_price   = EXCLUDED.sell_price,
            ts           = EXCLUDED.ts
    """, discord_id, int(sale.trade_id), sale.player_name, sale.card_version, sale.buy_price, sale.sell_price, ts)

    # 2) Mirror into main trades so the dashboard shows it immediately
    player   = sale.player_name
    version  = str(sale.card_version or "Standard")
    qty      = 1
    buy      = int(sale.buy_price or 0)
    sell     = int(sale.sell_price or 0)
    platform = "ps"     # pick a default; user can edit in UI
    tag      = "fut-webapp"
    ea_tax   = int(round(sell * qty * 0.05))
    profit   = (sell - buy) * qty

    await conn.execute("""
        INSERT INTO trades (
            user_id, player, version, buy, sell, quantity, platform,
            profit, ea_tax, tag, notes, timestamp, trade_id
        )
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
        ON CONFLICT (user_id, trade_id)
        DO UPDATE SET
            sell      = EXCLUDED.sell,
            buy       = EXCLUDED.buy,
            profit    = EXCLUDED.profit,
            ea_tax    = EXCLUDED.ea_tax,
            version   = EXCLUDED.version,
            platform  = EXCLUDED.platform,
            tag       = EXCLUDED.tag,
            timestamp = EXCLUDED.timestamp
    """, discord_id, player, version, buy, sell, qty, platform,
         profit, ea_tax, tag, "", ts, int(sale.trade_id))

    return {"ok": True}

# mount it
app.include_router(ext_router)


# ----------------- ROUTES -----------------
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
    state = secrets.token_urlsafe(24)
    OAUTH_STATE[state] = {"flow": "dashboard", "ts": time.time()}
    params = {
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": DISCORD_REDIRECT_URI,
        "response_type": "code",
        "scope": "identify",
        "state": state,
        "prompt": "none",
    }
    return RedirectResponse(f"{DISCORD_OAUTH_AUTHORIZE}?{urlencode(params)}")

@app.get("/api/price-history")
async def price_history(playerId: int, platform: str = "ps", tf: str = "today"):
    if playerId <= 0:
        raise HTTPException(status_code=400, detail="playerId must be a positive integer")
    try:
        return await get_price_history(playerId, platform, tf)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Upstream error: {e}")

@app.get("/api/callback")
async def callback(request: Request):
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    if not code:
        raise HTTPException(status_code=400, detail="Missing code")

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
            async with session.post(DISCORD_OAUTH_TOKEN, data=data, headers=headers) as resp:
                if resp.status != 200:
                    txt = await resp.text()
                    raise HTTPException(status_code=400, detail=f"OAuth token exchange failed: {txt}")
                token_data = await resp.json()
                access_token = token_data.get("access_token")
                if not access_token:
                    raise HTTPException(status_code=400, detail="OAuth failed")

        user_data = await get_discord_user_info(access_token)
        if not user_data:
            raise HTTPException(status_code=400, detail="Failed to fetch user data")

        user_id = user_data["id"]

        if state and state in OAUTH_STATE and OAUTH_STATE.get(state, {}).get("flow") != "dashboard":
            meta = OAUTH_STATE.pop(state)
            jwt_token = issue_extension_token(user_id)
            ext_redirect = meta["ext_redirect"]
            return RedirectResponse(f"{ext_redirect}#token={jwt_token}&state={state}")

        if state and state in OAUTH_STATE:
            OAUTH_STATE.pop(state, None)

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
                user_id, 0,
            )
            await conn.execute(
                """
                INSERT INTO user_profiles (user_id, username, avatar_url, global_name, updated_at)
                VALUES ($1, $2, $3, $4, NOW())
                ON CONFLICT (user_id)
                DO UPDATE SET username = $2, avatar_url = $3, global_name = $4, updated_at = NOW()
                """,
                user_id, username, avatar_url, global_name,
            )

        return RedirectResponse(f"{FRONTEND_URL}/auth/done")
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"OAuth error: {e}")
        raise HTTPException(status_code=500, detail="Authentication failed")

@app.get("/api/logout")
async def logout_get(request: Request):
    request.session.clear()
    return {"message": "Logged out successfully"}

@app.post("/api/logout")
async def logout_post(request: Request):
    request.session.clear()
    return {"message": "Logged out successfully"}

# OAuth – Chrome extension
@app.get("/oauth/start")
async def oauth_start(redirect_uri: str):
    if not redirect_uri.startswith("https://") or "chromiumapp.org" not in redirect_uri:
        raise HTTPException(400, "Invalid redirect_uri")
    state = secrets.token_urlsafe(24)
    OAUTH_STATE[state] = {"ext_redirect": redirect_uri, "ts": time.time(), "flow": "extension"}
    params = {
        "client_id": DISCORD_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": DISCORD_REDIRECT_URI,
        "scope": "identify",
        "state": state,
        "prompt": "consent",
    }
    return RedirectResponse(f"{DISCORD_OAUTH_AUTHORIZE}?{urlencode(params)}")


# ----------------- BUSINESS ROUTES -----------------
async def fetch_dashboard_data(user_id: str, conn):
    portfolio = await conn.fetchrow("SELECT starting_balance FROM portfolio WHERE user_id=$1", user_id)
    stats = await conn.fetchrow(
        "SELECT COALESCE(SUM(profit),0) AS total_profit, COALESCE(SUM(ea_tax),0) AS total_tax, COUNT(*) as total_trades FROM trades WHERE user_id=$1",
        user_id,
    )
    trades = await conn.fetch(
        "SELECT player, version, buy, sell, quantity, platform, profit, ea_tax, tag, timestamp, trade_id "
        "FROM trades WHERE user_id=$1 ORDER BY timestamp DESC LIMIT 10",
        user_id,
    )
    all_trades = await conn.fetch("SELECT profit FROM trades WHERE user_id=$1 ORDER BY timestamp DESC", user_id)
    win_count = len([t for t in all_trades if t["profit"] and t["profit"] > 0])
    win_rate = round((win_count / len(all_trades)) * 100, 1) if all_trades else 0
    tag_stats = await conn.fetch(
        "SELECT tag, COUNT(*) as count FROM trades WHERE user_id=$1 GROUP BY tag ORDER BY count DESC LIMIT 1",
        user_id,
    )
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
        },
    }

@app.get("/api/dashboard")
async def get_dashboard(user_id: str = Depends(get_current_user), conn=Depends(get_db)):
    return await fetch_dashboard_data(user_id, conn)

@app.get("/api/profile")
async def get_profile(user_id: str = Depends(get_current_user), conn=Depends(get_db)):
    return await fetch_dashboard_data(user_id, conn)

@app.post("/api/trades")
async def add_trade(request: Request, user_id: str = Depends(get_current_user), conn=Depends(get_db)):
    data = await request.json()
    required_fields = ["player", "version", "buy", "sell", "quantity", "platform", "tag"]
    missing_fields = [f for f in required_fields if f not in data or data[f] == ""]
    if missing_fields:
        raise HTTPException(status_code=400, detail=f"Missing required fields: {missing_fields}")

    try:
        quantity = int(data["quantity"])
        buy = parse_coin_amount(data["buy"])
        sell = parse_coin_amount(data["sell"])
        if quantity <= 0 or buy < 0 or sell <= 0:
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid numeric values")

    # ✅ trade_id: sanitize or generate
    trade_id = data.get("trade_id")
    if isinstance(trade_id, str):
        tid = trade_id.strip()
        trade_id = int(tid) if tid.isdigit() else None
    elif not isinstance(trade_id, (int, type(None))):
        trade_id = None

    if trade_id is None:
        # generate snowflake-ish id + ensure uniqueness for this user
        base = int(time.time() * 1000)
        trade_id = base + secrets.randbelow(1000)
        exists = await conn.fetchval(
            "SELECT 1 FROM trades WHERE user_id=$1 AND trade_id=$2",
            user_id, trade_id
        )
        if exists:
            trade_id = base + secrets.randbelow(1000000)

    profit = (sell - buy) * quantity
    ea_tax = int(round(sell * quantity * 0.05))

    row = await conn.fetchrow(
        """
        INSERT INTO trades (
            user_id, player, version, buy, sell, quantity, platform, profit, ea_tax, tag, notes, timestamp, trade_id
        )
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,NOW(),$12)
        RETURNING player, version, buy, sell, quantity, platform, profit, ea_tax, tag, notes, timestamp, trade_id
        """,
        user_id,
        (data["player"] or "").strip(),
        (data["version"] or "").strip(),
        buy,
        sell,
        quantity,
        (data["platform"] or "").strip(),
        profit,
        ea_tax,
        (data["tag"] or "").strip(),
        (data.get("notes", "") or "").strip(),
        trade_id,
    )
    return {
        "message": "Trade added successfully!",
        "trade": dict(row)
    }

@app.get("/api/trades")
async def get_all_trades(user_id: str = Depends(get_current_user), conn=Depends(get_db)):
    rows = await conn.fetch("SELECT * FROM trades WHERE user_id=$1 ORDER BY timestamp DESC", user_id)
    return {"trades": [dict(r) for r in rows]}

@app.put("/api/trades/{trade_id}")
async def update_trade(
    trade_id: int,
    payload: 'TradeUpdate',
    user_id: str = Depends(get_current_user),
    conn=Depends(get_db),
):
    data = payload.model_dump(exclude_none=True)
    if not data:
        raise HTTPException(status_code=400, detail="No fields to update")

    if "buy" in data:
        data["buy"] = parse_coin_amount(data["buy"])
    if "sell" in data:
        data["sell"] = parse_coin_amount(data["sell"])

    fields, values = [], []
    for col, val in data.items():
        fields.append(f"{col} = ${len(values)+1}")
        values.append(val)

    q = f"""
        UPDATE trades
           SET {', '.join(fields)}
         WHERE trade_id = ${len(values)+1}
           AND user_id  = ${len(values)+2}
     RETURNING player, version, quantity, buy, sell, platform, tag, notes, ea_tax, profit, timestamp, trade_id
    """
    values.extend([trade_id, user_id])
    row = await conn.fetchrow(q, *values)
    if not row:
        raise HTTPException(status_code=404, detail="Trade not found")

    sell = int(row["sell"] or 0)
    buy  = int(row["buy"] or 0)
    qty  = int(row["quantity"] or 1)
    ea_tax = int(round(sell * qty * 0.05))
    profit = (sell - buy) * qty

    row2 = await conn.fetchrow(
        """
        UPDATE trades
           SET ea_tax = $1,
               profit = $2
         WHERE trade_id = $3
           AND user_id  = $4
     RETURNING player, version, quantity, buy, sell, platform, tag, notes, ea_tax, profit, timestamp, trade_id
        """,
        ea_tax, profit, trade_id, user_id
    )
    return dict(row2)

@app.delete("/api/trades/{trade_id}")
async def delete_trade(trade_id: int, user_id: str = Depends(get_current_user), conn=Depends(get_db)):
    result = await conn.execute("DELETE FROM trades WHERE trade_id=$1 AND user_id=$2", trade_id, user_id)
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Trade not found")
    return {"message": "Trade deleted successfully"}

# Continue with remaining endpoints...
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
                    "Origin": "https://www.fut.gg",
                },
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                return {"error": f"API returned status {resp.status}"}
    except Exception as e:
        logging.error(f"Player definition fetch error: {e}")
        return {"error": str(e)}

# Watchlist alert helper functions
async def _send_discord_dm(user_discord_id: str, content: str) -> bool:
    if not DISCORD_BOT_TOKEN or not user_discord_id:
        return False
    try:
        async with aiohttp.ClientSession(headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}", "Content-Type":"application/json"}) as sess:
            async with sess.post("https://discord.com/api/v10/users/@me/channels", json={"recipient_id": user_discord_id}) as r:
                if r.status not in (200, 201):
                    return False
                ch = await r.json()
                ch_id = ch.get("id")
            async with sess.post(f"https://discord.com/api/v10/channels/{ch_id}/messages", json={"content": content}) as r2:
                return r2.status in (200, 201)
    except Exception as e:
        logging.warning("DM send failed: %s", e)
        return False

async def _send_channel_fallback(channel_id: Optional[str], content: str) -> bool:
    if not DISCORD_BOT_TOKEN:
        return False
    ch_id = channel_id or WATCHLIST_FALLBACK_CHANNEL_ID
    if not ch_id:
        return False
    try:
        async with aiohttp.ClientSession(headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}", "Content-Type":"application/json"}) as sess:
            async with sess.post(f"https://discord.com/api/v10/channels/{ch_id}/messages", json={"content": content}) as r:
                return r.status in (200, 201)
    except Exception as e:
        logging.warning("Channel send failed: %s", e)
        return False

async def _alerts_poll_loop():
    await asyncio.sleep(3)
    while True:
        try:
            async with watchlist_pool.acquire() as w:
                pairs = await w.fetch("SELECT DISTINCT card_id, platform FROM watchlist_alerts")
            tasks = []
            for rec in pairs:
                cid = int(rec["card_id"])
                plat = rec["platform"]
                tasks.append(_poll_pair_once(cid, plat))
            if tasks:
                await asyncio.gather(*tasks)
        except Exception as e:
            logging.warning("poll loop error: %s", e)
        await asyncio.sleep(WATCHLIST_POLL_INTERVAL)

async def _poll_pair_once(card_id: int, platform: str):
    try:
        live = await fetch_price(card_id, platform)
        price = live.get("price")
        if not isinstance(price, (int, float)):
            return
        # Simplified - you can expand this with your alert logic
        logging.debug(f"Polled {card_id}/{platform}: {price}")
    except Exception as e:
        logging.debug("poll pair error: %s", e)

# Add remaining endpoints...
@app.get("/api/me")
async def get_current_user_info(request: Request):
    uid = request.session.get("user_id")
    if not uid:
        return {"authenticated": False}
    return {
        "authenticated": True,
        "user_id": uid,
        "username": request.session.get("username"),
        "avatar_url": request.session.get("avatar_url"),
        "global_name": request.session.get("global_name"),
    }

# Include optional routers
try:
    from app.routers.squad import router as squad_router
    app.include_router(squad_router, prefix="/api")
    logging.info("✅ Squad router loaded")
except Exception as e:
    logging.warning("⚠️ Squad router not loaded: %s", e)

# Entry point
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT)
