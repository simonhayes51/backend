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
import stripe

from bs4 import BeautifulSoup
from types import SimpleNamespace
from urllib.parse import urlencode
from contextlib import asynccontextmanager, suppress
from typing import Any, Dict, List, Literal, Optional, Tuple, AsyncGenerator
from dotenv import load_dotenv
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta, timezone, time as dt_time

from fastapi import (
    FastAPI, APIRouter, Request, HTTPException, Depends,
    UploadFile, File, Query
)
from fastapi.responses import RedirectResponse, JSONResponse, StreamingResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel, Field

from app.auth.entitlements import compute_entitlements, require_feature
from app.services.price_history import get_price_history
from app.services.prices import get_player_price  # optional
from app.routers.smart_buy import router as smart_buy_router
from app.routers.trade_finder import router as trade_finder_router
from app.routers.auth_me import router as auth_me_router
from discord_manager import discord_manager
from app.routers.market import router as market_router
from app.routers.ai_engine import router as ai_router
from app.routers.players import router as players_router


# ----------------- BOOTSTRAP -----------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("app")
load_dotenv()

# --------- ENV ---------
required_env_vars = ["DATABASE_URL", "SECRET_KEY"]
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
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://app.futhub.co.uk").rstrip("/")
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

# Stripe
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

# Watchlist alert env
WATCHLIST_FALLBACK_CHANNEL_ID = os.getenv("WATCHLIST_FALLBACK_CHANNEL_ID")
WATCHLIST_POLL_INTERVAL = int(os.getenv("WATCHLIST_POLL_INTERVAL", "60"))  # seconds

# ephemeral state store
OAUTH_STATE: Dict[str, Dict[str, Any]] = {}

def _prune_oauth_state(ttl: int = 600) -> None:
    now = time.time()
    stale = [k for k, v in OAUTH_STATE.items() if now - v.get("ts", 0) > ttl]
    for k in stale:
        OAUTH_STATE.pop(k, None)

# --------- FUT.GG / Watchlist config ---------
FUTGG_BASE = "https://www.fut.gg/api/fut/player-prices/26"
PRICE_CACHE_TTL = 5  # seconds
_price_cache: Dict[str, Dict[str, Any]] = {}

# ----------------- FUT.GG MOMENTUM -----------------
MOMENTUM_BASE = "https://www.fut.gg/players/momentum"
MOMENTUM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Referer": "https://www.fut.gg/",
}
_CARD_HREF_RE = re.compile(r"/players/(\d+)-[a-z0-9-]+/26-(\d+)/?", re.IGNORECASE)

def _norm_tf(tf: Optional[str]) -> str:
    if not tf:
        return "24"
    tf = tf.lower().strip()
    if tf.endswith("h"):
        tf = tf[:-1]
    return tf if tf in ("6", "12", "24") else "24"

# Optional: cache momentum pages to reduce upstream load
_MOMENTUM_CACHE: dict[tuple[str, int], dict] = {}
MOMENTUM_TTL = 120  # seconds

# Market summary cache
_MARKET_SUMMARY_CACHE: dict = {}
MARKET_SUMMARY_TTL = 90  # seconds

# Shared HTTP session
HTTP_SESSION: Optional[aiohttp.ClientSession] = None

async def _fetch_momentum_page(tf: str, page: int) -> str:
    now = time.time()
    key = (tf, page)
    hit = _MOMENTUM_CACHE.get(key)
    if hit and (now - hit["at"] < MOMENTUM_TTL):
        return hit["html"]

    url = f"{MOMENTUM_BASE}/{tf}/?page={page}"
    timeout = aiohttp.ClientTimeout(total=12)
    sess = HTTP_SESSION or aiohttp.ClientSession(timeout=timeout, headers=MOMENTUM_HEADERS)
    must_close = sess is not HTTP_SESSION
    try:
        async with sess.get(url, headers=MOMENTUM_HEADERS, timeout=timeout) as r:
            if r.status != 200:
                raise HTTPException(status_code=502, detail=f"MOMENTUM {r.status}")
            html = await r.text()
    finally:
        if must_close:
            await sess.close()

    _MOMENTUM_CACHE[key] = {"html": html, "at": now}
    return html

# ---- Market summary (cached) -----------------------------------------------
market_summary_router = APIRouter()

async def get_player_db(request: Request) -> AsyncGenerator:
    pool = getattr(request.app.state, "player_pool", None)
    if pool is None:
        raise RuntimeError("player_pool not initialized")
    async with pool.acquire() as conn:
        yield conn

@market_summary_router.get("/api/market/summary")
async def market_summary(tf: str = "24", rise: float = 5.0, fall: float = 5.0):
    """
    Quick snapshot of the market using FUT.GG momentum pages.
    - tf: "6" | "12" | "24"
    - rise: % threshold to count as 'trending'
    - fall: % threshold to count as 'falling' (absolute, e.g. 5 -> <= -5%)
    """
    tf_norm = _norm_tf(tf)
    key = (tf_norm, float(rise), float(fall))
    now = time.time()

    hit = _MARKET_SUMMARY_CACHE.get(key)
    if hit and (now - hit["at"] < MARKET_SUMMARY_TTL):
        return hit["data"]

    # Look at a small slice of the list: first 3 and last 3 pages
    try:
        html1 = await _fetch_momentum_page(tf_norm, 1)
    except Exception:
        # serve stale cache if available; else empty snapshot
        if hit:
            return hit["data"]
        return {"tf": f"{tf_norm}h", "sample": 0, "trending": 0, "falling": 0, "stable": 0}

    last = _parse_last_page_number(html1)
    pages = sorted({*range(1, min(last, 3) + 1), *range(max(1, last - 2), last + 1)})

    percents: list[float] = []
    seen: set[int] = set()
    for p in pages:
        items, _ = await _momentum_page_items(tf_norm, p)
        for it in items:
            cid = int(it["card_id"])
            if cid in seen:
                continue
            seen.add(cid)
            try:
                percents.append(float(it["percent"]))
            except Exception:
                continue

    trending = sum(1 for v in percents if v >= rise)
    falling  = sum(1 for v in percents if v <= -abs(fall))
    stable   = max(0, len(percents) - trending - falling)

    data = {
        "tf": f"{tf_norm}h",
        "sample": len(percents),
        "trending": trending,
        "falling": falling,
        "stable": stable,
    }
    _MARKET_SUMMARY_CACHE[key] = {"at": now, "data": data}
    return data

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
        try: return int(round(float(s[:-2]) * 1_000_000))
        except: return 0
    if s.endswith("k"):
        try: return int(round(float(s[:-1]) * 1_000))
        except: return 0
    if s.endswith("m"):
        try: return int(round(float(s[:-1]) * 1_000_000))
        except: return 0
    try:
        return int(round(float(s)))
    except:
        return 0

# ========== SBC HELPERS ==========
from typing import List, Dict, Any, Tuple, Optional  # (kept for local scope usage)
from collections import Counter
import datetime as dt

def _band_filter_sql(exact_bronze: bool, exact_silver: bool, exact_gold: bool) -> str:
    if exact_bronze:
        return " AND p.rating <= 64 "
    if exact_silver:
        return " AND p.rating BETWEEN 65 AND 74 "
    if exact_gold:
        return " AND p.rating >= 75 "
    return ""

def _ensure_slots_array(raw_positions: Any) -> List[str]:
    if raw_positions is None:
        return []
    if isinstance(raw_positions, list):
        return [str(x).strip().upper() for x in raw_positions]
    return [s.strip().upper() for s in str(raw_positions).split(",") if s.strip()]

def _counts(assigns: List[Dict[str, Any]]) -> Tuple[Counter, Counter, Counter, int]:
    nat = Counter(a.get("nation_id") or a.get("nation") for a in assigns)
    lg  = Counter(a.get("league_id") or a.get("league") for a in assigns)
    cl  = Counter(a.get("club_id")   or a.get("club")   for a in assigns)
    rare = sum(1 for a in assigns if str(a.get("rarity","")).lower().find("rare") >= 0)
    return nat, lg, cl, rare

def _validate(assigns: List[Dict[str, Any]], ch: Dict[str, Any]) -> Tuple[bool, List[str]]:
    msgs = []
    if len(assigns) != 11:
        msgs.append("squad not complete")
        return False, msgs

    avg_rating = sum(a["rating"] for a in assigns) / 11.0
    if ch.get("min_squad_rating") and avg_rating < ch["min_squad_rating"]:
        msgs.append(f"avg rating {avg_rating:.1f} < {ch['min_squad_rating']}")

    nat, lg, cl, rare = _counts(assigns)

    def _lt(val, req, label):
        if req and val < req: msgs.append(f"{label} {val} < {req}")

    def _maxlt(counter, req, label):
        if req and (max(counter.values() or [0]) < req):
            msgs.append(f"{label} max {max(counter.values() or [0])} < {req}")

    _lt(len(nat), ch.get("min_nations"), "nations")
    _lt(len(lg),  ch.get("min_leagues"), "leagues")
    _lt(len(cl),  ch.get("min_clubs"), "clubs")
    _maxlt(cl, ch.get("min_same_club"), "same club")
    _maxlt(lg, ch.get("min_same_league"), "same league")
    _maxlt(nat, ch.get("min_same_nation"), "same nation")
    _lt(rare, ch.get("rare_players"), "rare")

    return (len(msgs) == 0), msgs

def _estimate_chem(assigns: List[Dict[str, Any]]) -> int:
    _, lg, cl, _ = _counts(assigns)
    league_cluster = max(lg.values() or [0])
    club_cluster   = max(cl.values() or [0])
    base = 15 + 2*league_cluster + 1*club_cluster
    return max(0, min(33, base))

async def _fetch_candidates_for_slot(
    con,
    req_pos: str,
    ch: Dict[str, Any],
    account_id: Optional[int],
    use_club_only: bool,
    prefer_untradeable: bool,
    limit: int,
) -> List[Dict[str, Any]]:
    band = []
    if ch.get("exact_bronze"):
        band.append("p.rating <= 64")
    if ch.get("exact_silver"):
        band.append("p.rating BETWEEN 65 AND 74")
    if ch.get("exact_gold"):
        band.append("p.rating >= 75")
    band_sql = (" AND " + " AND ".join(band)) if band else ""

    sql = f"""
      SELECT
        p.card_id, p.name, p.rating, p.position,
        p.price_num
      FROM fut_players p
      WHERE p.position = $1
        {band_sql}
        AND p.price_num IS NOT NULL
      ORDER BY p.price_num ASC, p.rating ASC
      LIMIT $2
    """
    rows = await con.fetch(sql, req_pos, limit)

    out = []
    for r in rows:
        out.append({
            "card_id": r["card_id"],
            "name": r["name"],
            "rating": r["rating"],
            "primary_pos": r["position"],
            "price": r["price_num"] or 999_999_999,
            "raw_price": r["price_num"],
            "source": "market",
        })
    return out

async def _solve_challenge(
    con,
    ch_row: Any,
    account_id: Optional[int],
    use_club_only: bool,
    prefer_untradeable: bool,
    max_candidates_per_slot: int = 100,
) -> Dict[str, Any]:
    ch = dict(ch_row)
    slots = _ensure_slots_array(ch.get("positions"))
    if len(slots) != 11:
        return {"ok": False, "error": "invalid_positions"}

    picks: List[Dict[str, Any]] = []
    used_ids = set()

    for req_pos in slots:
        cands = await _fetch_candidates_for_slot(
            con, req_pos, ch, account_id, use_club_only, prefer_untradeable, max_candidates_per_slot
        )
        chosen = None
        for c in cands:
            if c["card_id"] in used_ids:
                continue
            if c["primary_pos"] == req_pos:
                chosen = {**c, "used_pos": req_pos}
                break
        if not chosen:
            return {"ok": False, "error": f"no_candidate_for_{req_pos}"}
        used_ids.add(chosen["card_id"])
        picks.append(chosen)

    ok, reasons = _validate(picks, ch)

    chem_est = _estimate_chem(picks)
    total_cost = int(sum(p["price"] for p in picks))

    summary = {
        "avg_rating": round(sum(p["rating"] for p in picks) / 11.0, 2),
        "nations": len(_counts(picks)[0]),
        "leagues": len(_counts(picks)[1]),
        "clubs":   len(_counts(picks)[2]),
        "chem_estimate": chem_est,
    }

    return {
        "ok": ok,
        "error": (None if ok else "; ".join(reasons)),
        "total_cost": total_cost,
        "summary": summary,
        "picks": [
            {
                "slot": i+1,
                "req_pos": slots[i],
                "card_id": p["card_id"],
                "name": p["name"],
                "rating": p["rating"],
                "used_pos": p["used_pos"],
                "price": p["price"],
                "source": p["source"],
            } for i, p in enumerate(picks)
        ],
        "price_snapshot": dt.datetime.utcnow(),
    }

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

# --------- Pydantic models ---------
class UserSettings(BaseModel):
    default_platform: str = "Console"
    custom_tags: List[str] = Field(default_factory=list)
    currency_format: str = "coins"
    theme: str = "dark"
    timezone: str = "UTC"
    date_format: str = "US"
    include_tax_in_profit: bool = True
    default_chart_range: str = "30d"
    visible_widgets: List[str] = Field(default_factory=lambda: ["profit", "tax", "balance", "trades"])

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
    platform: str  # "ps" | "xbox" | "pc"
    notes: Optional[str] = None

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

# ---- Pools / lifespan ----
pool = None
player_pool = None
watchlist_pool = None
_watchlist_task: Optional[asyncio.Task] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool, player_pool, watchlist_pool, _watchlist_task, HTTP_SESSION

    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    player_pool = pool if PLAYER_DATABASE_URL == DATABASE_URL else await asyncpg.create_pool(PLAYER_DATABASE_URL, min_size=1, max_size=10)
    watchlist_pool = pool if WATCHLIST_DATABASE_URL == DATABASE_URL else await asyncpg.create_pool(WATCHLIST_DATABASE_URL, min_size=1, max_size=10)

    # Shared HTTP session
    HTTP_SESSION = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))

    # Expose pools
    app.state.pool = pool
    app.state.player_pool = player_pool
    app.state.watchlist_pool = watchlist_pool

    # ---------- Core tables (create-first so fresh DBs work) ----------
    async with pool.acquire() as conn:
        # trades
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            user_id TEXT NOT NULL,
            player TEXT NOT NULL,
            version TEXT NOT NULL,
            buy INTEGER NOT NULL,
            sell INTEGER NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 1,
            platform TEXT NOT NULL,
            profit INTEGER NOT NULL DEFAULT 0,
            ea_tax INTEGER NOT NULL DEFAULT 0,
            tag TEXT,
            notes TEXT,
            timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            trade_id BIGINT
        )""")
        await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS trades_user_trade_uidx ON trades (user_id, trade_id)")
        await conn.execute("DROP INDEX IF EXISTS idx_trades_date")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_user_ts ON trades(user_id, timestamp)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_tag ON trades(user_id, tag)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_platform ON trades(user_id, platform)")

        # users (plan/premium/roles read by compute_entitlements)
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
          id TEXT PRIMARY KEY,
          plan TEXT,
          premium_until TIMESTAMPTZ,
          roles JSONB DEFAULT '[]'
        )""")

        # portfolio
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS portfolio (
            user_id TEXT PRIMARY KEY,
            starting_balance INTEGER NOT NULL DEFAULT 0
        )""")

        # usersettings
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
        )""")

        # user_profiles
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS user_profiles (
            id SERIAL PRIMARY KEY,
            user_id VARCHAR(255) UNIQUE NOT NULL,
            username VARCHAR(255),
            avatar_url TEXT,
            global_name VARCHAR(255),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")

        # Add premium status to user_profiles
        await conn.execute("""
        ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS is_premium BOOLEAN DEFAULT FALSE
        """)
        await conn.execute("""
        ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS premium_until TIMESTAMP WITH TIME ZONE
        """)

        # Billing tables
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            id SERIAL PRIMARY KEY,
            user_id VARCHAR(255) NOT NULL REFERENCES user_profiles(user_id),
            stripe_subscription_id VARCHAR(255) UNIQUE,
            stripe_customer_id VARCHAR(255),
            status VARCHAR(50) NOT NULL DEFAULT 'active',
            plan_id VARCHAR(255) NOT NULL,
            current_period_start TIMESTAMP WITH TIME ZONE,
            current_period_end TIMESTAMP WITH TIME ZONE,
            cancel_at_period_end BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        ) """)
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id SERIAL PRIMARY KEY,
            user_id VARCHAR(255) NOT NULL,
            subscription_id INTEGER REFERENCES subscriptions(id),
            stripe_payment_intent_id VARCHAR(255),
            amount INTEGER NOT NULL,
            currency VARCHAR(3) DEFAULT 'GBP',
            status VARCHAR(50) NOT NULL,
            created_at TIMESTAMP DEFAULT NOW()
        ) """)
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS discord_roles (
            id SERIAL PRIMARY KEY,
            user_id VARCHAR(255) NOT NULL,
            discord_user_id VARCHAR(255) NOT NULL,
            role_id VARCHAR(255) NOT NULL,
            assigned_at TIMESTAMP DEFAULT NOW(),
            expires_at TIMESTAMP
        ) """)

        # trading_goals
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
        )""")

        # backfill trade_id if NULL (compat)
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

        # fut_trades raw ingest
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS fut_trades (
          id           BIGSERIAL PRIMARY KEY,
          discord_id   TEXT NOT NULL,
          trade_id     BIGINT NOT NULL,
          player_name  TEXT NOT NULL,
          card_version TEXT,
          buy_price    INTEGER,
          sell_price   INTEGER NOT NULL,
          ts           TIMESTAMPTZ NOT NULL,
          source       TEXT DEFAULT 'webapp'
        )""")
        await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS fut_trades_uidx ON fut_trades (discord_id, trade_id)")

        # events (Next Promo)
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

        # Smart Buy tables
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
            created_at TIMESTAMPTZ DEFAULT NOW(),
            expires_at TIMESTAMPTZ
        )""")
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
            timestamp TIMESTAMPTZ DEFAULT NOW()
        )""")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_smart_buy_feedback_user_action ON smart_buy_feedback(user_id, action)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_smart_buy_feedback_card ON smart_buy_feedback(card_id)")

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS market_states (
            id BIGSERIAL PRIMARY KEY,
            platform VARCHAR(10) NOT NULL,
            state VARCHAR(30) NOT NULL,
            confidence_score INTEGER NOT NULL,
            detected_at TIMESTAMPTZ DEFAULT NOW(),
            indicators JSONB
        )""")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_market_states_platform_detected ON market_states(platform, detected_at DESC)")

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS smart_buy_market_cache (
            id SMALLINT PRIMARY KEY DEFAULT 1,
            payload JSONB NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""")

    # watchlist DB objects (on watchlist_pool)
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
        )""")
        await wconn.execute("CREATE INDEX IF NOT EXISTS idx_watchlist_user ON watchlist(user_id)")
        await wconn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_watchlist_unique
        ON watchlist(user_id, card_id, platform)
        """)

        await wconn.execute("""
        CREATE TABLE IF NOT EXISTS watchlist_alerts (
          id BIGSERIAL PRIMARY KEY,
          user_id TEXT NOT NULL,
          user_discord_id TEXT,
          card_id BIGINT NOT NULL,
          platform TEXT NOT NULL CHECK (platform IN ('ps','xbox','pc')),
          ref_mode TEXT NOT NULL DEFAULT 'last_close',
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

    # Start alerts loop (defined later)
    _watchlist_task = asyncio.create_task(_alerts_poll_loop())
    logging.info("✅ Watchlist alerts loop started (%ss)", WATCHLIST_POLL_INTERVAL)

    try:
        yield
    finally:
        if _watchlist_task:
            _watchlist_task.cancel()
            with suppress(asyncio.CancelledError):
                await _watchlist_task
        for p in {pool, player_pool, watchlist_pool}:
            if p is not None:
                await p.close()
        if HTTP_SESSION:
            await HTTP_SESSION.close()

# --- FastAPI app ---
app = FastAPI(lifespan=lifespan)

from app.routers.watchlist import router as watchlist_router
app.include_router(watchlist_router)

# Local DB dependency (use the pools created in lifespan)
async def get_db():
    if not hasattr(app.state, "pool") or app.state.pool is None:
        raise HTTPException(500, "Database pool not initialized")
    async with app.state.pool.acquire() as conn:
        yield conn

class SbcSolveReq(BaseModel):
    challenge_code: str
    account_id: Optional[int] = None
    use_club_only: bool = False
    prefer_untradeable: bool = True
    max_candidates_per_slot: int = 100

@app.get("/api/sbc/challenge/{code}")
async def sbc_get_challenge(code: str, conn=Depends(get_db)):
    ch = await conn.fetchrow("SELECT * FROM sbc_challenges WHERE challenge_code=$1", code)
    if not ch:
        return {"ok": False, "error": "challenge_not_found"}
    return {"ok": True, "challenge": dict(ch)}

@app.post("/api/sbc/solve")
async def sbc_solve(req: SbcSolveReq, conn=Depends(get_db)):
    ch = await conn.fetchrow("SELECT * FROM sbc_challenges WHERE challenge_code=$1", req.challenge_code)
    if not ch:
        return {"ok": False, "error": "challenge_not_found"}

    sol = await _solve_challenge(
        conn, ch, req.account_id, req.use_club_only, req.prefer_untradeable, req.max_candidates_per_slot
    )
    return sol

@app.get("/api/entitlements")
async def get_entitlements(request: Request):
    """Get user's current entitlements with real-time validation"""
    uid = request.session.get("user_id")
    if not uid:
        return {
            "user_id": None,
            "is_premium": False,
            "features": [],
            "limits": {
                "watchlist_max": 3,
                "trending": {"timeframes": ["24h"], "limit": 5, "smart": False}
            },
            "roles": []
        }

    # Use app.state pool created by lifespan
    async with request.app.state.pool.acquire() as conn:
        is_premium = False
        premium_until = None

        active_subscription = await conn.fetchrow(
            """
            SELECT current_period_end, status, cancel_at_period_end
            FROM subscriptions
            WHERE user_id = $1
              AND status IN ('active', 'trialing', 'trial')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            uid
        )

        if active_subscription:
            current_period_end = active_subscription["current_period_end"]
            now = datetime.now(timezone.utc)
            if current_period_end and current_period_end > now:
                is_premium = True
                premium_until = current_period_end
            else:
                await conn.execute(
                    """
                    UPDATE user_profiles
                    SET is_premium = FALSE, premium_until = NULL, updated_at = NOW()
                    WHERE user_id = $1
                    """,
                    uid
                )
                try:
                    await discord_manager.remove_premium_role(uid)
                except Exception as e:
                    logging.warning(f"Failed to remove Discord role for expired user {uid}: {e}")

        return {
            "user_id": uid,
            "is_premium": is_premium,
            "premium_until": premium_until.isoformat() if premium_until else None,
            "features": ["smart_buy", "trade_finder", "deal_confidence", "backtest", "smart_trending"] if is_premium else [],
            "limits": {
                "watchlist_max": 500 if is_premium else 3,
                "trending": {
                    "timeframes": ["4h", "6h", "24h"] if is_premium else ["24h"],
                    "limit": 20 if is_premium else 5,
                    "smart": is_premium
                }
            },
            "roles": ["Premium"] if is_premium else [],
            "last_validated": datetime.now(timezone.utc).isoformat()
        }

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://app.futhub.co.uk",
        "https://www.futhub.co.uk",
        "https://futhub.co.uk",
        "https://api.futhub.co.uk",
        "http://localhost:5173",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL],
    allow_credentials=True,           # cookies!
    allow_methods=["GET","POST","PUT","PATCH","DELETE","OPTIONS"],
    allow_headers=["Content-Type","Authorization","X-Requested-With"],
    expose_headers=[],                # optional
)

from starlette.middleware.sessions import SessionMiddleware
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    same_site="none",
    https_only=True,
    session_cookie="session",
)

# ---------------- Routers & helpers ----------------

router = APIRouter()

@router.get("/api/auth/callback")
async def auth_callback(request: Request, code: str, state: str | None = None):
    # ...exchange code with Discord, load/create user...
    discord_id = "<resolved_user_id>"

    # Write to the session (SessionMiddleware will emit Set-Cookie on this response)
    request.session.update({"uid": discord_id, "iat": int(time.time())})

    # Redirect the user to your app
    return RedirectResponse(url=f"{FRONTEND_ORIGIN}/auth/done")

# DB dependencies (use pools created in lifespan)
async def get_db(request: Request):
    pool = getattr(request.app.state, "pool", None)
    if not pool:
        raise HTTPException(500, "Database pool not initialized")
    async with pool.acquire() as connection:
        yield connection

async def get_watchlist_db(request: Request):
    pool = getattr(request.app.state, "watchlist_pool", None)
    if not pool:
        raise HTTPException(500, "Watchlist pool not initialized")
    async with pool.acquire() as connection:
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

async def get_member_role_names(discord_user_id: str) -> list[str]:
    if not (DISCORD_BOT_TOKEN and DISCORD_SERVER_ID):
        return []
    try:
        async with aiohttp.ClientSession(headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}) as s:
            # Member -> role ids
            async with s.get(f"https://discord.com/api/guilds/{DISCORD_SERVER_ID}/members/{discord_user_id}") as r:
                if r.status != 200:
                    return []
                member = await r.json()
                role_ids = set(str(x) for x in (member.get("roles") or []))

            # Guild roles -> map id->name
            async with s.get(f"https://discord.com/api/guilds/{DISCORD_SERVER_ID}/roles") as r2:
                if r2.status != 200:
                    return []
                roles_meta = await r2.json()
                id_to_name = {str(rr["id"]): rr["name"] for rr in roles_meta}

        return [id_to_name.get(rid, rid) for rid in role_ids]
    except Exception:
        return []


# --- Premium by Discord Role ---
DISCORD_PREMIUM_ROLE_ID = os.getenv("DISCORD_PREMIUM_ROLE_ID")

_ROLE_CACHE: dict[str, dict] = {}
ROLE_CACHE_TTL = 300  # 5 minutes

async def user_has_premium_role(user_id: str) -> bool:
    """Return True if the member has the Premium role in your guild (cached)."""
    if not (DISCORD_BOT_TOKEN and DISCORD_SERVER_ID and DISCORD_PREMIUM_ROLE_ID):
        return False

    now = time.time()
    hit = _ROLE_CACHE.get(user_id)
    if hit and (now - hit["at"] < ROLE_CACHE_TTL):
        return bool(hit["ok"])

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://discord.com/api/v10/guilds/{DISCORD_SERVER_ID}/members/{user_id}",
                headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
            ) as resp:
                if resp.status != 200:
                    ok = False
                else:
                    js = await resp.json()
                    roles = js.get("roles") or []
                    ok = DISCORD_PREMIUM_ROLE_ID in roles
    except Exception:
        ok = False

    _ROLE_CACHE[user_id] = {"ok": ok, "at": now}
    return ok


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
    scope = payload.get("scope")
    if scope != "trade:ingest":
        raise HTTPException(status_code=403, detail="Insufficient scope")
    return SimpleNamespace(discord_id=payload.get("sub"))

# (fetch_price defined earlier in the file — keep only one definition in main.py)

ext_router = APIRouter()

@ext_router.get("/ext/ping")
async def ext_ping(auth = Depends(require_extension_jwt)):
    return {"ok": True, "sub": auth.discord_id}

@ext_router.post("/ext/trades")
async def ext_add_trade(
    sale: ExtSale,
    auth = Depends(require_extension_jwt),
    conn = Depends(get_db),
):
    discord_id = auth.discord_id or "unknown"
    ts = datetime.fromtimestamp(int(sale.timestamp_ms) / 1000, tz=timezone.utc)

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

    player   = sale.player_name
    version  = str(sale.card_version or "Standard")
    qty      = 1
    buy      = int(sale.buy_price or 0)
    sell     = int(sale.sell_price or 0)
    platform = "ps"
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

# ---- Router wiring (single, final) ----
app.include_router(auth_me_router)          # /api/auth/me
app.include_router(trade_finder_router)     # /api/trade-finder...
app.include_router(ext_router)              # /ext/...

# Import-based market router (candles/indicators)
app.include_router(market_router)           # /api/market/*

# Local summary router
app.include_router(market_summary_router)   # /api/market/summary

# AI Engine
app.include_router(ai_router)               # /api/ai/*
app.include_router(players_router)

# Premium-only — mount at /api/smart-buy
app.include_router(
    smart_buy_router,
    prefix="/api",
    dependencies=[Depends(require_feature("smart_buy"))],
)

@app.get("/")
async def root():
    return {"message": "FUT Dashboard API", "status": "healthy"}

@app.get("/health")
async def health_check(request: Request):
    try:
        async with request.app.state.pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return {"status": "healthy", "database": "connected"}
    except Exception as e:
        return {"status": "unhealthy", "database": "disconnected", "error": str(e)}

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
        "prompt": "consent",
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

# REPLACE your existing callback function with this
@app.get("/api/callback")
async def callback(request: Request):
    err = request.query_params.get("error")
    err_desc = request.query_params.get("error_description")
    if err:
        raise HTTPException(400, detail=f"OAuth error from Discord: {err}: {err_desc or ''}".strip())

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
                txt = await resp.text()
                if resp.status != 200:
                    logging.error("Discord token exchange failed (%s): %s", resp.status, txt[:500])
                    raise HTTPException(400, detail=f"OAuth token exchange failed: {txt}")
                token_data = json.loads(txt)

        access_token = token_data.get("access_token")
        if not access_token:
            logging.error("No access_token in token_data: %s", token_data)
            raise HTTPException(400, detail="OAuth failed (no access_token)")

        user_data = await get_discord_user_info(access_token)
        if not user_data or "id" not in user_data:
            logging.error("Failed to fetch user data: %s", user_data)
            raise HTTPException(400, detail="Failed to fetch user data")
        user_id = user_data["id"]

        if state and state in OAUTH_STATE and OAUTH_STATE.get(state, {}).get("flow") != "dashboard":
            meta = OAUTH_STATE.pop(state, None) or {}
            jwt_token = issue_extension_token(user_id)
            ext_redirect = meta.get("ext_redirect")
            if not ext_redirect:
                raise HTTPException(400, detail="Invalid extension state")
            return RedirectResponse(f"{ext_redirect}#token={jwt_token}&state={state}")

        if state:
            OAUTH_STATE.pop(state, None)

        is_member = await check_server_membership(user_id)
        if not is_member:
            return RedirectResponse(f"{FRONTEND_URL}/access-denied")

        username = f"{user_data.get('username','user')}#{user_data.get('discriminator', '0000')}"
        avatar_url = (
            f"https://cdn.discordapp.com/avatars/{user_id}/{user_data['avatar']}.png"
            if user_data.get('avatar')
            else f"https://cdn.discordapp.com/embed/avatars/{int(user_data.get('discriminator','0') or 0) % 5}.png"
        )
        global_name = user_data.get('global_name') or user_data.get('username') or "User"

        request.session["user_id"] = user_id
        request.session["username"] = username
        request.session["avatar_url"] = avatar_url
        request.session["global_name"] = global_name
        request.session["roles"] = await get_member_role_names(user_id)

        return RedirectResponse(f"{FRONTEND_URL}/auth/done")

    except HTTPException:
        raise
    except Exception as e:
        logging.exception("OAuth unexpected error: %s", e)
        raise HTTPException(500, detail="Authentication failed")

@app.get("/api/logout")
async def logout_get(request: Request):
    request.session.clear()
    return {"message": "Logged out successfully"}

@app.post("/api/logout")
async def logout_post(request: Request):
    request.session.clear()
    return {"message": "Logged out successfully"}

@app.get("/oauth/start")
async def oauth_start(redirect_uri: str):
    _prune_oauth_state()
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
    missing_fields = [f for f in required_fields if f not in data or f"{data[f]}" == ""]
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

    trade_id = data.get("trade_id")
    if isinstance(trade_id, str):
        tid = trade_id.strip()
        trade_id = int(tid) if tid.isdigit() else None
    elif not isinstance(trade_id, (int, type(None))):
        trade_id = None

    if trade_id is None:
        base = int(time.time() * 1000)
        trade_id = base + secrets.randbelow(1000)
        exists = await conn.fetchval("SELECT 1 FROM trades WHERE user_id=$1 AND trade_id=$2", user_id, trade_id)
        if exists:
            trade_id = base + secrets.randbelow(1000000)

    profit = (sell - buy) * quantity
    ea_tax = int(round(sell * quantity * 0.05))

    row = await conn.fetchrow("""
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
    return {"message": "Trade added successfully!", "trade": dict(row)}

@app.get("/api/trades")
async def get_all_trades(user_id: str = Depends(get_current_user), conn=Depends(get_db)):
    rows = await conn.fetch("SELECT * FROM trades WHERE user_id=$1 ORDER BY timestamp DESC", user_id)
    return {"trades": [dict(r) for r in rows]}

@app.put("/api/trades/{trade_id}")
async def update_trade(
    trade_id: int,
    payload: TradeUpdate,
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

    row2 = await conn.fetchrow("""
        UPDATE trades
           SET ea_tax = $1,
               profit = $2
         WHERE trade_id = $3
           AND user_id  = $4
     RETURNING player, version, quantity, buy, sell, platform, tag, notes, ea_tax, profit, timestamp, trade_id
    """, ea_tax, profit, trade_id, user_id)
    return dict(row2)

@app.delete("/api/trades/{trade_id}")
async def delete_trade(trade_id: int, user_id: str = Depends(get_current_user), conn=Depends(get_db)):
    result = await conn.execute("DELETE FROM trades WHERE trade_id=$1 AND user_id=$2", trade_id, user_id)
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Trade not found")
    return {"message": "Trade deleted successfully"}

@app.get("/api/fut-player-definition/{card_id}")
async def get_player_definition(card_id: str):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://www.fut.gg/api/fut/player-item-definitions/26/{card_id}/",
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

@app.get("/api/fut-player-price/{card_id}")
async def get_player_price_proxy(card_id: str):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://www.fut.gg/api/fut/player-prices/26/{card_id}",
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
        logging.error(f"Player price fetch error: {e}")
        return {"error": str(e)}

@app.get("/api/sbc/candidates")
async def sbc_candidates(
    req_pos: str,
    exact_bronze: bool = False,
    exact_silver: bool = False,
    exact_gold: bool = False,
    allow_alt_pos: bool = True,
    limit: int = 10,
    user_id: str = Depends(get_current_user),
    conn=Depends(get_db),
):
    """
    Return cheapest candidate players for a given SBC slot.
    """
    band_sql = _band_filter_sql(exact_bronze, exact_silver, exact_gold)
    pos_check = "p.position=$1 OR $1 = ANY(p.alt_positions)" if allow_alt_pos else "p.position=$1"

    sql = f"""
      SELECT p.card_id, p.name, p.rating, p.position, p.alt_positions,
             p.nation, p.club, p.league,
             p.price_num AS price
        FROM fut_players p
       WHERE ({pos_check})
         {band_sql}
         AND p.price_num IS NOT NULL
       ORDER BY p.price_num ASC, p.rating ASC
       LIMIT $2
    """

    rows = await conn.fetch(sql, req_pos, limit)
    return [dict(r) for r in rows]

@app.get("/api/watchlist/usage")
async def watchlist_usage(request: Request, user_id: str = Depends(get_current_user)):
    """
    Return current watchlist usage vs plan limit.
    """
    ent = await compute_entitlements(request)
    async with request.app.state.watchlist_pool.acquire() as conn:
        used = await conn.fetchval(
            "SELECT COUNT(*) FROM watchlist WHERE user_id=$1", user_id
        )
    return {
        "used": int(used or 0),
        "max": int(ent["limits"]["watchlist_max"]),
        "is_premium": bool(ent["is_premium"]),
    }

@app.get("/api/watchlist")
async def list_watch_items(request: Request, user_id: str = Depends(get_current_user)):
    """
    List all watchlist items with live price, change stats, and lightweight player meta.
    Optimized to fetch live prices concurrently.
    """
    try:
        async with request.app.state.watchlist_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM watchlist WHERE user_id=$1 ORDER BY started_at DESC",
                user_id,
            )

        watches = [dict(r) for r in rows]
        if not watches:
            return {"ok": True, "items": []}

        # Batch meta lookup
        card_ids: list[str] = [
            str(w["card_id"]) for w in watches if w.get("card_id") is not None
        ]
        async with request.app.state.player_pool.acquire() as pconn:
            meta_rows = await pconn.fetch(
                """
                SELECT card_id, name, rating, club, nation
                FROM fut_players
                WHERE card_id = ANY($1::text[])
                """,
                card_ids,
            )

        meta_map = {
            str(m["card_id"]): {
                "name": m["name"],
                "rating": m["rating"],
                "club": m["club"],
                "nation": m["nation"],
            }
            for m in meta_rows
        }

        # Fetch live prices concurrently
        tasks = [
            fetch_price(int(w["card_id"]), (w["platform"] or "ps").lower())
            for w in watches
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        enriched = []
        for w, live in zip(watches, results):
            live_dict = live if isinstance(live, dict) else {}
            live_price = live_dict.get("price") if isinstance(live_dict, dict) else None

            change = None
            change_pct = None
            if isinstance(live_price, (int, float)) and w["started_price"] and int(w["started_price"]) > 0:
                change = int(live_price) - int(w["started_price"])
                change_pct = round((change / int(w["started_price"])) * 100, 2)

            m = meta_map.get(str(w["card_id"]), {})
            enriched.append(
                {
                    "id": w["id"],
                    "card_id": w["card_id"],
                    "player_name": w["player_name"],
                    "version": w["version"],
                    "platform": w["platform"],
                    "started_price": w["started_price"],
                    "started_at": w["started_at"].isoformat(),
                    "current_price": int(live_price) if isinstance(live_price, (int, float)) else None,
                    "is_extinct": bool(live_dict.get("isExtinct", False)),
                    "updated_at": live_dict.get("updatedAt"),
                    "change": change,
                    "change_pct": change_pct,
                    "notes": w["notes"],
                    "name": m.get("name"),
                    "rating": m.get("rating"),
                    "club": m.get("club"),
                    "nation": m.get("nation"),
                }
            )

        return {"ok": True, "items": enriched}
    except Exception as e:
        logging.exception("Watchlist GET error")
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/watchlist/{watch_id}")
async def delete_watch_item(
    request: Request, watch_id: int, user_id: str = Depends(get_current_user)
):
    """
    Remove a single watchlist item.
    """
    try:
        async with request.app.state.watchlist_pool.acquire() as conn:
            res = await conn.execute(
                "DELETE FROM watchlist WHERE id=$1 AND user_id=$2", watch_id, user_id
            )
            if res == "DELETE 0":
                raise HTTPException(status_code=404, detail="Watch item not found")
            return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        logging.exception("Watchlist DELETE error")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/watchlist/{watch_id}/refresh")
async def refresh_watch_item(
    request: Request, watch_id: int, user_id: str = Depends(get_current_user)
):
    """
    Re-fetch and persist the latest price for a single watchlist item, returning the updated snapshot.
    """
    try:
        async with request.app.state.watchlist_pool.acquire() as conn:
            w = await conn.fetchrow(
                "SELECT * FROM watchlist WHERE id=$1 AND user_id=$2", watch_id, user_id
            )
            if not w:
                raise HTTPException(status_code=404, detail="Watch item not found")

            plat = (w["platform"] or "ps").lower()
            live = await fetch_price(int(w["card_id"]), plat)
            val = live.get("price")
            live_price = int(val) if isinstance(val, (int, float)) else None

            await conn.execute(
                "UPDATE watchlist SET last_price=$1, last_checked=NOW() WHERE id=$2",
                live_price,
                watch_id,
            )

            change = None
            change_pct = None
            if isinstance(live_price, (int, float)) and int(w["started_price"] or 0) > 0:
                change = int(live_price) - int(w["started_price"])
                change_pct = round((change / int(w["started_price"])) * 100, 2)

        async with request.app.state.player_pool.acquire() as pconn:
            meta = await pconn.fetchrow(
                """
                SELECT card_id, name, rating, club, nation
                FROM fut_players
                WHERE card_id = $1::text
                """,
                str(w["card_id"]),
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
                "is_extinct": bool(live.get("isExtinct", False)),
                "updated_at": live.get("updatedAt"),
                "change": change,
                "change_pct": change_pct,
                "notes": w["notes"],
                "name": meta_dict.get("name"),
                "rating": meta_dict.get("rating"),
                "club": meta_dict.get("club"),
                "nation": meta_dict.get("nation"),
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        logging.exception("Watchlist REFRESH error")
        raise HTTPException(status_code=500, detail=str(e))

async def _send_discord_dm(user_discord_id: str, content: str) -> bool:
    if not DISCORD_BOT_TOKEN or not user_discord_id:
        return False
    try:
        async with aiohttp.ClientSession(
            headers={
                "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
                "Content-Type": "application/json",
            }
        ) as sess:
            async with sess.post(
                "https://discord.com/api/v10/users/@me/channels",
                json={"recipient_id": user_discord_id},
            ) as r:
                if r.status not in (200, 201):
                    return False
                ch = await r.json()
                ch_id = ch.get("id")
            async with sess.post(
                f"https://discord.com/api/v10/channels/{ch_id}/messages",
                json={"content": content},
            ) as r2:
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
        async with aiohttp.ClientSession(
            headers={
                "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
                "Content-Type": "application/json",
            }
        ) as sess:
            async with sess.post(
                f"https://discord.com/api/v10/channels/{ch_id}/messages",
                json={"content": content},
            ) as r:
                return r.status in (200, 201)
    except Exception as e:
        logging.warning("Channel send failed: %s", e)
        return False

def _fmt_alert(
    name: str,
    platform: str,
    direction: str,
    pct: float,
    price: float,
    ref_mode: str,
    ref_price: Optional[float],
) -> str:
    arrow = "📈" if direction == "rise" else "📉"
    rp = f"{int(ref_price):,}c" if isinstance(ref_price, (int, float)) else "—"
    return (
        f"{arrow} Watchlist Alert • {name} ({platform.upper()})\n"
        f"Current: {int(price):,}c • Change: {pct:+.2f}%\n"
        f"Ref: {rp} ({ref_mode})"
    )

async def _ref_price_for_alert(row: asyncpg.Record) -> Optional[float]:
    mode = row["ref_mode"]
    if mode == "fixed" and row["ref_price"]:
        return float(row["ref_price"])
    if mode == "started_price":
        async with watchlist_pool.acquire() as w:
            r = await w.fetchrow(
                "SELECT started_price FROM watchlist WHERE user_id=$1 AND card_id=$2 AND platform=$3",
                row["user_id"],
                row["card_id"],
                row["platform"],
            )
        return float(r["started_price"]) if r and r["started_price"] else None
    try:
        hist = await get_price_history(int(row["card_id"]), row["platform"], "today")
        if hist:
            p = hist[-1]
            v = p.get("price") or p.get("v") or p.get("y")
            return float(v) if v else None
    except Exception:
        return None
    return None

async def _resolve_player_name(card_id: int) -> str:
    try:
        async with player_pool.acquire() as p:
            r = await p.fetchrow(
                "SELECT name FROM fut_players WHERE card_id=$1::text", str(card_id)
            )
        return r["name"] if r and r["name"] else f"Card {card_id}"
    except Exception:
        return f"Card {card_id}"

async def _eval_alerts_for_pair(card_id: int, platform: str, price_now: float) -> int:
    sent = 0
    now = now_utc()
    async with watchlist_pool.acquire() as w:
        rows = await w.fetch(
            "SELECT * FROM watchlist_alerts WHERE card_id=$1 AND platform=$2",
            card_id,
            platform,
        )
    if not rows:
        return 0

    name = await _resolve_player_name(card_id)
    for row in rows:
        try:
            if row["quiet_start"] or row["quiet_end"]:
                if is_within_quiet_hours(now, row["quiet_start"], row["quiet_end"]):
                    continue

            last = row["last_alert_at"]
            if last and (now - last).total_seconds() < (row["cooloff_minutes"] * 60):
                continue

            refp = await _ref_price_for_alert(row)
            if not refp:
                continue

            pct = 100.0 * (price_now - refp) / refp
            direction = None
            if pct >= float(row["rise_pct"] or 0):
                direction = "rise"
            elif pct <= -float(row["fall_pct"] or 0):
                direction = "fall"
            if not direction:
                continue

            content = _fmt_alert(
                name, platform, direction, pct, price_now, row["ref_mode"], refp
            )
            ok = False
            if row["prefer_dm"] and row["user_discord_id"]:
                ok = await _send_discord_dm(row["user_discord_id"], content)
            if not ok:
                await _send_channel_fallback(row["fallback_channel_id"], content)

            async with watchlist_pool.acquire() as w:
                await w.execute(
                    """
                    INSERT INTO alerts_log (
                        user_id, user_discord_id, card_id, platform, direction,
                        pct, price, ref_mode, ref_price
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                    """,
                    row["user_id"],
                    row["user_discord_id"],
                    card_id,
                    platform,
                    direction,
                    pct,
                    price_now,
                    row["ref_mode"],
                    refp,
                )
                await w.execute(
                    "UPDATE watchlist_alerts SET last_alert_at=$1 WHERE id=$2",
                    now,
                    row["id"],
                )
            sent += 1
        except Exception as e:
            logging.warning("alert eval error: %s", e)
    return sent

async def _alerts_poll_loop():
    await asyncio.sleep(3)
    while True:
        try:
            async with watchlist_pool.acquire() as w:
                pairs = await w.fetch(
                    "SELECT DISTINCT card_id, platform FROM watchlist_alerts"
                )
            tasks = [
                _poll_pair_once(int(rec["card_id"]), rec["platform"])
                for rec in pairs
            ]
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
        n = await _eval_alerts_for_pair(card_id, platform, float(price))
        if n:
            logging.info("sent %s alerts for %s/%s", n, card_id, platform)
    except Exception as e:
        logging.debug("poll pair error: %s", e)

@app.get("/api/watchlist-alerts")
async def list_watchlist_alerts(user_id: str = Depends(get_current_user)):
    async with watchlist_pool.acquire() as w:
        rows = await w.fetch(
            "SELECT * FROM watchlist_alerts WHERE user_id=$1 ORDER BY created_at DESC",
            user_id,
        )
    return {"items": [dict(r) for r in rows]}

@app.post("/api/watchlist-alerts")
async def create_watchlist_alert(
    payload: WatchlistAlertCreate, user_id: str = Depends(get_current_user)
):
    qs = None
    qe = None
    if payload.quiet_start:
        try:
            qs = datetime.strptime(payload.quiet_start, "%H:%M").time()
        except Exception:
            qs = None
    if payload.quiet_end:
        try:
            qe = datetime.strptime(payload.quiet_end, "%H:%M").time()
        except Exception:
            qe = None

    plat = (payload.platform or "ps").lower()
    if plat not in ("ps", "xbox", "pc"):
        plat = "ps"

    async with watchlist_pool.acquire() as w:
        await w.execute(
            """
            INSERT INTO watchlist_alerts (
                user_id, user_discord_id, card_id, platform, ref_mode, ref_price,
                rise_pct, fall_pct, cooloff_minutes, quiet_start, quiet_end,
                prefer_dm, fallback_channel_id
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
            """,
            user_id,
            user_id,  # DM by default to the same Discord ID (if used)
            int(payload.card_id),
            plat,
            payload.ref_mode or "last_close",
            payload.ref_price,
            payload.rise_pct or 5,
            payload.fall_pct or 5,
            payload.cooloff_minutes or 30,
            qs,
            qe,
            bool(payload.prefer_dm),
            payload.fallback_channel_id,
        )
    return {"ok": True}

@app.delete("/api/watchlist-alerts/{alert_id}")
async def delete_watchlist_alert(
    alert_id: int, user_id: str = Depends(get_current_user)
):
    async with watchlist_pool.acquire() as w:
        res = await w.execute(
            "DELETE FROM watchlist_alerts WHERE id=$1 AND user_id=$2", alert_id, user_id
        )
    if res == "DELETE 0":
        raise HTTPException(404, "Alert not found")
    return {"ok": True}

@app.post("/api/watchlist-alerts/test")
async def test_alert_endpoint(
    card_id: int,
    platform: str = "ps",
    price: Optional[int] = None,
    user_id: str = Depends(get_current_user),
):
    plat = (platform or "ps").lower()
    if price is None:
        live = await fetch_price(card_id, plat)
        price = live.get("price")
    if not isinstance(price, (int, float)):
        raise HTTPException(400, "No price available")
    n = await _eval_alerts_for_pair(card_id, plat, float(price))
    return {"sent": n}

@app.get("/api/me")
async def get_current_user_info(request: Request, conn=Depends(get_db)):
    uid = request.session.get("user_id")
    if not uid:
        return {"authenticated": False}

    # Get user profile
    profile = await conn.fetchrow(
        "SELECT username, avatar_url, global_name, is_premium, premium_until FROM user_profiles WHERE user_id = $1",
        uid
    )

    if not profile:
        return {"authenticated": False}

    # REAL-TIME PREMIUM VALIDATION
    is_premium = False
    premium_until = None

    # Check active subscription in real-time
    active_subscription = await conn.fetchrow(
        """
        SELECT current_period_end, status, cancel_at_period_end
        FROM subscriptions
        WHERE user_id = $1
          AND status IN ('active', 'trialing', 'trial')
        ORDER BY created_at DESC
        LIMIT 1
        """,
        uid
    )

    if active_subscription:
        current_period_end = active_subscription["current_period_end"]
        now = datetime.now(timezone.utc)

        # Check if subscription is still valid
        if current_period_end and current_period_end > now:
            is_premium = True
            premium_until = current_period_end
        else:
            # Subscription expired - update database immediately
            await conn.execute(
                """
                UPDATE user_profiles
                SET is_premium = FALSE, premium_until = NULL, updated_at = NOW()
                WHERE user_id = $1
                """,
                uid
            )

            # Also mark subscription as expired if needed
            await conn.execute(
                """
                UPDATE subscriptions
                SET status = 'past_due'
                WHERE user_id = $1 AND current_period_end <= $2 AND status = 'active'
                """,
                uid, now
            )

            # Remove Discord role immediately
            try:
                await discord_manager.remove_premium_role(uid)
            except Exception as e:
                logger.warning(f"Failed to remove Discord role for expired user {uid}: {e}")

    # If database shows premium but no active subscription, fix it
    elif profile["is_premium"]:
        is_premium = False
        await conn.execute(
            """
            UPDATE user_profiles
            SET is_premium = FALSE, premium_until = NULL, updated_at = NOW()
            WHERE user_id = $1
            """,
            uid
        )

    return {
        "authenticated": True,
        "user_id": uid,
        "username": profile["username"],
        "avatar_url": profile["avatar_url"],
        "global_name": profile["global_name"],
        "is_premium": is_premium,
        "premium_until": premium_until.isoformat() if premium_until else None,
        "discord_id": uid,
        "last_validated": datetime.now(timezone.utc).isoformat(),
        "features": ["smart_buy", "trade_finder", "advanced_analytics"] if is_premium else [],
        "limits": {
            "watchlist_max": 500 if is_premium else 3,
            "trending": {"timeframes": ["4h", "6h", "24h"] if is_premium else ["24h"]}
        }
    }

@app.get("/api/validate-premium")
async def validate_premium(user_id: str = Depends(get_current_user), conn=Depends(get_db)):
    """Fast endpoint to validate premium status without full user data"""
    active_subscription = await conn.fetchrow(
        """
        SELECT current_period_end, status
        FROM subscriptions
        WHERE user_id = $1
          AND status IN ('active', 'trialing', 'trial')
          AND current_period_end > NOW()
        ORDER BY created_at DESC
        LIMIT 1
        """,
        user_id
    )

    is_premium = bool(active_subscription)

    # If no active subscription but user marked as premium, fix it
    if not is_premium:
        await conn.execute(
            """
            UPDATE user_profiles
            SET is_premium = FALSE, premium_until = NULL, updated_at = NOW()
            WHERE user_id = $1 AND is_premium = TRUE
            """,
            user_id
        )

    return {
        "is_premium": is_premium,
        "validated_at": datetime.now(timezone.utc).isoformat()
    }

@app.get("/api/settings")
async def get_user_settings(
    user_id: str = Depends(get_current_user), conn=Depends(get_db)
):
    """Get user settings with proper defaults and validation"""
    try:
        settings_row = await conn.fetchrow(
            """
            SELECT
                default_platform,
                custom_tags,
                currency_format,
                theme,
                timezone,
                date_format,
                include_tax_in_profit,
                default_chart_range,
                visible_widgets,
                created_at,
                updated_at
            FROM usersettings
            WHERE user_id = $1
        """,
            user_id,
        )

        if settings_row:
            return {
                "default_platform": settings_row["default_platform"] or "Console",
                "custom_tags": settings_row["custom_tags"] or [],
                "currency_format": settings_row["currency_format"] or "coins",
                "theme": settings_row["theme"] or "dark",
                "timezone": settings_row["timezone"] or "UTC",
                "date_format": settings_row["date_format"] or "US",
                "include_tax_in_profit": settings_row["include_tax_in_profit"] if settings_row["include_tax_in_profit"] is not None else True,
                "default_chart_range": settings_row["default_chart_range"] or "30d",
                "visible_widgets": settings_row["visible_widgets"] or ["profit", "tax", "balance", "trades"],
                "created_at": settings_row["created_at"].isoformat() if settings_row["created_at"] else None,
                "updated_at": settings_row["updated_at"].isoformat() if settings_row["updated_at"] else None,
            }
        else:
            # Return defaults for new users
            default_settings = {
                "default_platform": "Console",
                "custom_tags": [],
                "currency_format": "coins",
                "theme": "dark",
                "timezone": "UTC",
                "date_format": "US",
                "include_tax_in_profit": True,
                "default_chart_range": "30d",
                "visible_widgets": ["profit", "tax", "balance", "trades"],
                "created_at": None,
                "updated_at": None,
            }

            # Create default settings in database
            await conn.execute(
                """
                INSERT INTO usersettings (
                    user_id, default_platform, custom_tags, currency_format, theme,
                    timezone, date_format, include_tax_in_profit, default_chart_range,
                    visible_widgets, created_at, updated_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, NOW(), NOW())
            """,
                user_id,
                default_settings["default_platform"],
                json.dumps(default_settings["custom_tags"]),
                default_settings["currency_format"],
                default_settings["theme"],
                default_settings["timezone"],
                default_settings["date_format"],
                default_settings["include_tax_in_profit"],
                default_settings["default_chart_range"],
                json.dumps(default_settings["visible_widgets"]),
            )

            return default_settings

    except Exception as e:
        logging.error(f"Error fetching user settings: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch settings")

@app.post("/api/settings")
async def update_user_settings(
    settings: UserSettings, user_id: str = Depends(get_current_user), conn=Depends(get_db)
):
    """Update user settings with validation and proper error handling"""
    try:
        # Validate settings
        if settings.default_platform not in ["Console", "Xbox", "PC"]:
            raise HTTPException(status_code=400, detail="Invalid platform")

        if settings.currency_format not in ["coins", "abbreviated", "decimal"]:
            raise HTTPException(status_code=400, detail="Invalid currency format")

        if settings.theme not in ["dark", "light", "system"]:
            raise HTTPException(status_code=400, detail="Invalid theme")

        if settings.date_format not in ["US", "EU", "ISO"]:
            raise HTTPException(status_code=400, detail="Invalid date format")

        if settings.default_chart_range not in ["7d", "30d", "90d", "1y"]:
            raise HTTPException(status_code=400, detail="Invalid chart range")

        # Update or insert settings
        await conn.execute(
            """
            INSERT INTO usersettings (
                user_id, default_platform, custom_tags, currency_format, theme,
                timezone, date_format, include_tax_in_profit, default_chart_range,
                visible_widgets, created_at, updated_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, NOW(), NOW())
            ON CONFLICT (user_id)
            DO UPDATE SET
                default_platform     = EXCLUDED.default_platform,
                custom_tags          = EXCLUDED.custom_tags,
                currency_format      = EXCLUDED.currency_format,
                theme                = EXCLUDED.theme,
                timezone             = EXCLUDED.timezone,
                date_format          = EXCLUDED.date_format,
                include_tax_in_profit= EXCLUDED.include_tax_in_profit,
                default_chart_range  = EXCLUDED.default_chart_range,
                visible_widgets      = EXCLUDED.visible_widgets,
                updated_at           = NOW()
        """,
            user_id,
            settings.default_platform,
            json.dumps(settings.custom_tags),
            settings.currency_format,
            settings.theme,
            settings.timezone,
            settings.date_format,
            settings.include_tax_in_profit,
            settings.default_chart_range,
            json.dumps(settings.visible_widgets),
        )

        return {"message": "Settings updated successfully", "timestamp": datetime.now().isoformat()}

    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error updating user settings: {e}")
        raise HTTPException(status_code=500, detail="Failed to update settings")

@app.post("/api/portfolio/balance")
async def update_starting_balance(
    request: Request, user_id: str = Depends(get_current_user), conn=Depends(get_db)
):
    data = await request.json()
    starting_balance = parse_coin_amount(data.get("starting_balance", 0))
    await conn.execute(
        "INSERT INTO portfolio (user_id, starting_balance) VALUES ($1, $2) "
        "ON CONFLICT (user_id) DO UPDATE SET starting_balance = $2",
        user_id,
        starting_balance,
    )
    return {"message": "Starting balance updated successfully"}

@app.get("/api/goals")
async def get_trading_goals(
    user_id: str = Depends(get_current_user), conn=Depends(get_db)
):
    goals = await conn.fetch(
        "SELECT * FROM trading_goals WHERE user_id=$1 ORDER BY created_at DESC",
        user_id,
    )
    return {"goals": [dict(g) for g in goals]}

@app.post("/api/goals")
async def create_trading_goal(
    goal: TradingGoal, user_id: str = Depends(get_current_user), conn=Depends(get_db)
):
    await conn.execute(
        """
        INSERT INTO trading_goals (user_id, title, target_amount, target_date, goal_type, is_completed, created_at)
        VALUES ($1, $2, $3, $4, $5, $6, NOW())
    """,
        user_id,
        goal.title,
        goal.target_amount,
        goal.target_date,
        goal.goal_type,
        goal.is_completed,
    )
    return {"message": "Goal created successfully"}

@app.get("/api/analytics/advanced")
async def get_advanced_analytics(
    user_id: str = Depends(get_current_user), conn=Depends(get_db)
):
    daily_profits = await conn.fetch(
        """
        SELECT DATE(timestamp) as date, COALESCE(SUM(profit), 0) as daily_profit, COUNT(*) as trades_count
        FROM trades WHERE user_id=$1 AND timestamp >= NOW() - INTERVAL '30 days'
        GROUP BY DATE(timestamp) ORDER BY date
    """,
        user_id,
    )
    tag_performance = await conn.fetch(
        """
        SELECT tag, COUNT(*) as trade_count, COALESCE(SUM(profit), 0) as total_profit,
               COALESCE(AVG(profit), 0) as avg_profit,
               COUNT(CASE WHEN profit > 0 THEN 1 END) * 100.0 / COUNT(*) as win_rate
        FROM trades WHERE user_id=$1 AND tag IS NOT NULL AND tag != ''
        GROUP BY tag ORDER BY total_profit DESC
    """,
        user_id,
    )
    platform_stats = await conn.fetch(
        """
        SELECT platform, COUNT(*) as trade_count, COALESCE(SUM(profit), 0) as total_profit,
               COALESCE(AVG(profit), 0) as avg_profit
        FROM trades WHERE user_id=$1 GROUP BY platform
    """,
        user_id,
    )
    monthly_summary = await conn.fetch(
        """
        SELECT DATE_TRUNC('month', timestamp) as month, COUNT(*) as trades_count,
               COALESCE(SUM(profit), 0) as total_profit, COALESCE(SUM(ea_tax), 0) as total_tax
        FROM trades WHERE user_id=$1
        GROUP BY DATE_TRUNC('month', timestamp) ORDER BY month DESC LIMIT 12
    """,
        user_id,
    )
    return {
        "daily_profits": [dict(r) for r in daily_profits],
        "tag_performance": [dict(r) for r in tag_performance],
        "platform_stats": [dict(r) for r in platform_stats],
        "monthly_summary": [dict(r) for r in monthly_summary],
    }

@app.put("/api/trades/bulk")
async def bulk_edit_trades(
    request: Request, user_id: str = Depends(get_current_user), conn=Depends(get_db)
):
    data = await request.json()
    trade_ids = data.get("trade_ids", [])
    updates = data.get("updates", {})
    if not trade_ids or not updates:
        raise HTTPException(status_code=400, detail="trade_ids and updates required")

    set_clauses: list[str] = []
    params: list[Any] = []

    if "tag" in updates:
        set_clauses.append(f"tag = ${len(params)+1}")
        params.append(updates["tag"])
    if "platform" in updates:
        set_clauses.append(f"platform = ${len(params)+1}")
        params.append(updates["platform"])

    if not set_clauses:
        raise HTTPException(status_code=400, detail="No valid updates provided")

    params.extend([user_id, trade_ids])

    query = (
        f"UPDATE trades SET {', '.join(set_clauses)} "
        f"WHERE user_id = ${len(params)-1} AND trade_id = ANY(${len(params)})"
    )

    await conn.execute(query, *params)
    return {"message": f"Updated {len(trade_ids)} trades successfully"}

@app.get("/api/export/trades")
async def export_trades(
    format: str = Query("csv", pattern="^(csv|json)$"),
    user_id: str = Depends(get_current_user),
    conn=Depends(get_db)
):
    """Export user trades with proper content-type headers"""
    try:
        rows = await conn.fetch(
            """
            SELECT
                trade_id,
                player,
                version,
                buy,
                sell,
                quantity,
                platform,
                profit,
                ea_tax,
                tag,
                notes,
                timestamp
            FROM trades
            WHERE user_id=$1
            ORDER BY timestamp DESC
            """,
            user_id
        )

        if not rows:
            # Return empty file for no data
            if format.lower() == "json":
                content = json.dumps([])
                media_type = "application/json"
                filename = "fut-trades-export.json"
            else:
                content = "No trades found"
                media_type = "text/csv"
                filename = "fut-trades-export.csv"

            return Response(
                content=content,
                media_type=media_type,
                headers={"Content-Disposition": f"attachment; filename={filename}"}
            )

        # Convert to list of dicts
        data = []
        for row in rows:
            trade_dict = dict(row)
            # Format timestamp for better readability
            if trade_dict['timestamp']:
                trade_dict['timestamp'] = trade_dict['timestamp'].isoformat()
            data.append(trade_dict)

        if format.lower() == "json":
            content = json.dumps(data, indent=2, default=str)
            return Response(
                content=content,
                media_type="application/json",
                headers={"Content-Disposition": "attachment; filename=fut-trades-export.json"}
            )
        else:
            # CSV export
            output = io.StringIO()
            if data:
                fieldnames = [
                    'trade_id', 'player', 'version', 'buy', 'sell', 'quantity',
                    'platform', 'profit', 'ea_tax', 'tag', 'notes', 'timestamp'
                ]
                writer = csv.DictWriter(output, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(data)

            return Response(
                content=output.getvalue(),
                media_type="text/csv",
                headers={"Content-Disposition": "attachment; filename=fut-trades-export.csv"}
            )

    except Exception as e:
        logging.error(f"Export error: {e}")
        raise HTTPException(status_code=500, detail="Failed to export data")

@app.post("/api/import/trades")
async def import_trades(
    file: UploadFile = File(...),
    user_id: str = Depends(get_current_user),
    conn=Depends(get_db),
):
    """Import trades with better error handling and validation"""
    try:
        if not file.filename:
            raise HTTPException(status_code=400, detail="No file provided")

        contents = await file.read()
        if not contents:
            raise HTTPException(status_code=400, detail="File is empty")

        fname = file.filename.lower()

        # Parse file based on extension
        try:
            if fname.endswith(".json"):
                payload = json.loads(contents.decode("utf-8"))
                trades_to_import = payload if isinstance(payload, list) else [payload]
            elif fname.endswith(".csv"):
                csv_content = contents.decode("utf-8")
                reader = csv.DictReader(io.StringIO(csv_content))
                trades_to_import = list(reader)
            else:
                raise HTTPException(status_code=400, detail="Unsupported file format. Use JSON or CSV.")
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON format")
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail="File encoding not supported. Use UTF-8.")
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Failed to parse file: {str(e)}")

        if not trades_to_import:
            raise HTTPException(status_code=400, detail="No trades found in file")

        imported_count = 0
        errors = []
        skipped_count = 0

        for i, trade_data in enumerate(trades_to_import):
            try:
                # Validate required fields
                player = (trade_data.get("player", "") or "").strip()
                version = (trade_data.get("version", "") or "Standard").strip()

                if not player:
                    errors.append(f"Row {i+1}: Missing player name")
                    continue

                # Parse numeric values
                try:
                    buy = parse_coin_amount(trade_data.get("buy", 0))
                    sell = parse_coin_amount(trade_data.get("sell", 0))
                    quantity = int(trade_data.get("quantity", 1))
                except (ValueError, TypeError):
                    errors.append(f"Row {i+1}: Invalid numeric values")
                    continue

                if buy <= 0 or sell <= 0 or quantity <= 0:
                    errors.append(f"Row {i+1}: Buy, sell, and quantity must be positive")
                    continue

                platform = (trade_data.get("platform", "Console") or "Console").strip()
                tag = (trade_data.get("tag", "") or "").strip()
                notes = (trade_data.get("notes", "") or "").strip()

                # Calculate profit and tax
                profit = (sell - buy) * quantity
                ea_tax = int(round(sell * quantity * 0.05))

                # Handle trade_id
                trade_id = None
                raw_tid = trade_data.get("trade_id")
                if raw_tid and str(raw_tid).isdigit():
                    trade_id = int(raw_tid)

                    # Check if trade_id already exists
                    existing = await conn.fetchval(
                        "SELECT 1 FROM trades WHERE user_id=$1 AND trade_id=$2",
                        user_id, trade_id
                    )
                    if existing:
                        skipped_count += 1
                        continue  # Skip duplicates

                if trade_id is None:
                    # Generate unique trade_id
                    base = int(time.time() * 1000)
                    trade_id = base + secrets.randbelow(1_000_000)

                    # Ensure uniqueness
                    while await conn.fetchval("SELECT 1 FROM trades WHERE user_id=$1 AND trade_id=$2", user_id, trade_id):
                        trade_id = base + secrets.randbelow(1_000_000)

                # Parse timestamp if provided
                timestamp = None
                if trade_data.get("timestamp"):
                    try:
                        timestamp = datetime.fromisoformat(trade_data["timestamp"].replace("Z", "+00:00"))
                    except:
                        timestamp = None

                if not timestamp:
                    timestamp = datetime.now(timezone.utc)

                # Insert trade
                await conn.execute(
                    """
                    INSERT INTO trades (
                        user_id, player, version, buy, sell, quantity, platform,
                        profit, ea_tax, tag, notes, timestamp, trade_id
                    )
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                    """
                    ,
                    user_id, player, version, buy, sell, quantity, platform,
                    profit, ea_tax, tag, notes, timestamp, trade_id
                )
                imported_count += 1

            except Exception as e:
                errors.append(f"Row {i+1}: {str(e)}")
                continue

        # Prepare response
        response = {
            "message": f"Import completed: {imported_count} trades imported",
            "imported_count": imported_count,
            "skipped_count": skipped_count,
            "error_count": len(errors),
            "total_processed": len(trades_to_import)
        }

        if errors:
            response["errors"] = errors[:20]  # Limit error list
            response["message"] += f", {len(errors)} errors"

        if skipped_count:
            response["message"] += f", {skipped_count} duplicates skipped"

        return response

    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Import trades error: {e}")
        raise HTTPException(status_code=500, detail="Import failed")

@app.delete("/api/data/delete-all")
async def delete_all_user_data(
    confirm: bool = Query(False),
    user_id: str = Depends(get_current_user),
    conn=Depends(get_db)
):
    """Delete all user data with confirmation"""
    if not confirm:
        raise HTTPException(status_code=400, detail="Confirmation required. Add ?confirm=true")

    try:
        # Start a transaction to ensure atomicity
        async with conn.transaction():
            # Delete trades
            trades_result = await conn.execute("DELETE FROM trades WHERE user_id=$1", user_id)
            trades_deleted = int(trades_result.split()[-1]) if trades_result.startswith("DELETE ") else 0

            # Delete goals
            goals_result = await conn.execute("DELETE FROM trading_goals WHERE user_id=$1", user_id)
            goals_deleted = int(goals_result.split()[-1]) if goals_result.startswith("DELETE ") else 0

            # Reset portfolio
            await conn.execute(
                "UPDATE portfolio SET starting_balance = 0 WHERE user_id=$1", user_id
            )

            # Delete settings (optional - user might want to keep preferences)
            settings_result = await conn.execute("DELETE FROM usersettings WHERE user_id=$1", user_id)
            settings_deleted = int(settings_result.split()[-1]) if settings_result.startswith("DELETE ") else 0

        return {
            "message": "All data deleted successfully",
            "details": {
                "trades_deleted": trades_deleted,
                "goals_deleted": goals_deleted,
                "settings_reset": settings_deleted > 0,
                "portfolio_reset": True
            },
            "timestamp": datetime.now().isoformat()
        }

    except Exception as e:
        logging.error(f"Delete all data error: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete data")

@app.get("/api/data/summary")
async def get_data_summary(
    user_id: str = Depends(get_current_user),
    conn=Depends(get_db)
):
    """Get summary of user's data for the settings page"""
    try:
        # Count trades
        trades_count = await conn.fetchval("SELECT COUNT(*) FROM trades WHERE user_id=$1", user_id)

        # Count goals
        goals_count = await conn.fetchval("SELECT COUNT(*) FROM trading_goals WHERE user_id=$1", user_id)

        # Get portfolio info
        portfolio = await conn.fetchrow("SELECT starting_balance FROM portfolio WHERE user_id=$1", user_id)

        # Get date range of trades
        date_range = await conn.fetchrow(
            "SELECT MIN(timestamp) as earliest, MAX(timestamp) as latest FROM trades WHERE user_id=$1",
            user_id
        )

        # Get total profit
        profit_info = await conn.fetchrow(
            "SELECT COALESCE(SUM(profit), 0) as total_profit, COALESCE(SUM(ea_tax), 0) as total_tax FROM trades WHERE user_id=$1",
            user_id
        )

        return {
            "trades_count": int(trades_count or 0),
            "goals_count": int(goals_count or 0),
            "starting_balance": int(portfolio["starting_balance"]) if portfolio else 0,
            "total_profit": int(profit_info["total_profit"]) if profit_info else 0,
            "total_tax": int(profit_info["total_tax"]) if profit_info else 0,
            "earliest_trade": date_range["earliest"].isoformat() if date_range and date_range["earliest"] else None,
            "latest_trade": date_range["latest"].isoformat() if date_range and date_range["latest"] else None,
        }

    except Exception as e:
        logging.error(f"Data summary error: {e}")
        raise HTTPException(status_code=500, detail="Failed to get data summary")

@app.get("/api/search-players")
async def search_players(request: Request, q: str = "", pos: Optional[str] = None):
    q = (q or "").strip()
    p = (pos or "").strip().upper() or None

    try:
        async with request.app.state.player_pool.acquire() as conn:
            where = []
            params: list[Any] = []

            if q:
                where.append("(LOWER(name) LIKE LOWER($1) OR card_id::text LIKE $1)")
                params.append(f"%{q}%")

            if p:
                params.append(p)
                idx = len(params)
                where.append(
                    f"""
                (
                  UPPER(position) = ${idx}
                  OR (
                    COALESCE(altposition, '') <> ''
                    AND EXISTS (
                      SELECT 1
                      FROM regexp_split_to_table(altposition, '[,;/|\\s]+') ap
                      WHERE UPPER(TRIM(ap)) = ${idx}
                    )
                  )
                )
                """
                )

            base_where = " AND ".join(where) if where else "TRUE"
            sql = f"""
                SELECT
                  card_id, name, rating, version, image_url, club, league, nation,
                  position, altposition, price
                FROM fut_players
                WHERE {base_where}
                ORDER BY
                  CASE WHEN price IS NULL THEN 1 ELSE 0 END,
                  rating DESC NULLS LAST,
                  name ASC
                LIMIT 50
            """

            rows = await conn.fetch(sql, *params)

        players = [
            {
                "card_id": int(r["card_id"]),
                "name": r["name"],
                "rating": r["rating"],
                "version": r["version"],
                "image_url": r["image_url"],
                "club": r["club"],
                "league": r["league"],
                "nation": r["nation"],
                "position": r["position"],
                "altposition": r["altposition"],
                "price": r["price"],
            }
            for r in rows
        ]

        return {"players": players}
    except Exception as e:
        logging.error(f"Player search error: {e}")
        return {"players": [], "error": str(e)}

@app.get("/api/debug/session")
async def debug_session(req: Request):
    return {
        "cookies_present": bool(req.cookies),
        "session_user_id": req.session.get("user_id"),
        "all_session_keys": list(req.session.keys()),
    }

async def _enrich_with_meta(items: list[dict]) -> list[dict]:
    if not items:
        return []
    ids = [str(it["card_id"]) for it in items]
    async with player_pool.acquire() as pconn:
        rows = await pconn.fetch(
            """
            SELECT card_id, name, rating, version, image_url, club, league
              FROM fut_players
             WHERE card_id = ANY($1::text[])
        """,
            ids,
        )
    meta = {str(r["card_id"]): dict(r) for r in rows}
    out = []
    for it in items:
        m = meta.get(str(it["card_id"]), {})
        out.append(
            {
                "pid": it["card_id"],
                "name": m.get("name") or f"Card {it['card_id']}",
                "rating": m.get("rating"),
                "version": m.get("version"),
                "image": m.get("image_url"),
                "club": m.get("club"),
                "league": m.get("league"),
                "percent": it["percent"],
                "price_ps": None,
                "price_xb": None,
            }
        )
    return out

async def _attach_prices_ps(items: list[dict]) -> list[dict]:
    # Use our unified FUT.GG fetcher for consistency
    tasks = [fetch_price(it["pid"], "ps") for it in items]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for it, val in zip(items, results):
        if isinstance(val, dict) and isinstance(val.get("price"), (int, float)):
            it["price_ps"] = int(val["price"])
        else:
            it["price_ps"] = None
    return items

@app.get("/api/trending")
async def api_trending(
    request: Request,
    type_: Optional[Literal["risers","fallers","smart"]] = Query(None, alias="type"),
    trend_type: Optional[Literal["risers","fallers","smart"]] = None,
    tf: Optional[str] = "24",
    limit: int = Query(10, ge=1, le=50),
):
    ent = await compute_entitlements(request)
    premium = bool(ent["is_premium"])
    allowed_tfs = {"6", "12", "24"} if premium else {"24"}

    tf_norm = _norm_tf(tf)
    limited = False

    kind = (type_ or trend_type or "fallers").lower()

    # Premium-only "smart"
    if kind == "smart" and not premium:
        raise HTTPException(
            status_code=402,
            detail={
                "error": "payment_required",
                "feature": "smart_trending",
                "message": "Smart Trending is a premium feature.",
                "upgrade_url": "/billing",
            },
        )

    # Coerce timeframe for free users
    if tf_norm not in allowed_tfs:
        tf_norm = "24"
        limited = True

    # Cap item count
    max_items = 20 if premium else 5
    if limit > max_items:
        limit = max_items
        limited = True

      # ---------- simple risers/fallers ----------
    if kind in ("fallers", "risers"):
        if kind == "fallers":
            items, _ = await _momentum_page_items(tf_norm, 1)
            items.sort(key=lambda x: x["percent"])
            pick = items[:limit]
        else:  # risers
            _, html = await _momentum_page_items(tf_norm, 1)
            last = _parse_last_page_number(html)
            items, _ = await _momentum_page_items(tf_norm, last)
            items.sort(key=lambda x: x["percent"], reverse=True)
            pick = items[:limit]

        enriched = await _enrich_with_meta(pick)
        enriched = await _attach_prices_ps(enriched)
        return {"type": kind, "timeframe": f"{tf_norm}h", "items": enriched, "limited": limited}

    # ---------- SMART MOVERS (6h vs 24h) ----------
    async def _page_last(tf_str: str) -> int:
        html1 = await _fetch_momentum_page(tf_str, 1)
        return _parse_last_page_number(html1)

    async def _top_sets(tf_str: str, pages_each_side: int = 5):
        """Return (fallers_map, risers_map) scanning first/last pages for a timeframe."""
        last = await _page_last(tf_str)
        fallers: dict[int, float] = {}
        risers:  dict[int, float] = {}

        # First pages ≈ fallers
        for p in range(1, min(last, pages_each_side) + 1):
            for it in _extract_items(await _fetch_momentum_page(tf_str, p)):
                fallers[int(it["card_id"])] = float(it["percent"])

        # Last pages ≈ risers
        for p in range(max(1, last - pages_each_side + 1), last + 1):
            for it in _extract_items(await _fetch_momentum_page(tf_str, p)):
                risers[int(it["card_id"])] = float(it["percent"])

        return fallers, risers

    f6, r6   = await _top_sets("6",  pages_each_side=5)
    f24, r24 = await _top_sets("24", pages_each_side=5)

    def _strongest(*maps: dict[int, float]) -> dict[int, float]:
        out: dict[int, float] = {}
        for mp in maps:
            for cid, pct in mp.items():
                if cid not in out or abs(pct) > abs(out[cid]):
                    out[cid] = pct
        return out

    p6  = _strongest(f6,  r6)
    p24 = _strongest(f24, r24)

    smart: list[tuple[int, float, float]] = []
    seen: set[int] = set()

    # 1) True sign flips first
    for cid, v6 in p6.items():
        v24 = p24.get(cid)
        if v24 is None:
            continue
        if v6 * v24 < 0:
            smart.append((cid, v6, v24))
            seen.add(cid)

    # 2) Fallback: biggest divergences (|6h - 24h|)
    if len(smart) < 10:
        diffs = []
        for cid, v6 in p6.items():
            v24 = p24.get(cid)
            if v24 is None or cid in seen:
                continue
            diffs.append((abs(v6 - v24), cid, v6, v24))
        diffs.sort(reverse=True)
        for diff, cid, v6, v24 in diffs:
            # require at least 5% divergence to avoid noise
            if diff < 5:
                break
            smart.append((cid, v6, v24))
            seen.add(cid)
            if len(smart) >= 10:
                break

    # 3) Final fill: largest combined magnitude
    if len(smart) < 10:
        fills = []
        for cid, v6 in p6.items():
            v24 = p24.get(cid)
            if v24 is None or cid in seen:
                continue
            fills.append((abs(v6) + abs(v24), cid, v6, v24))
        fills.sort(reverse=True)
        for _, cid, v6, v24 in fills:
            smart.append((cid, v6, v24))
            if len(smart) >= 10:
                break

    pick = [
        {"card_id": cid, "percent_6h": round(v6, 2), "percent_24h": round(v24, 2), "percent": round(v6, 2)}
        for cid, v6, v24 in smart[:limit]
    ]

    enriched = await _enrich_with_meta(pick)
    # attach smart fields
    meta_map = {it["pid"]: it for it in enriched}
    for raw in pick:
        pid = raw["card_id"]
        if pid in meta_map:
            meta_map[pid]["percent_6h"] = raw["percent_6h"]
            meta_map[pid]["percent_24h"] = raw["percent_24h"]
            meta_map[pid]["percent"] = raw["percent"]

    enriched = await _attach_prices_ps(list(meta_map.values()))
    return {"type": "smart", "timeframe": "6h_vs_24h", "items": enriched, "limited": limited}


def _cmp_now_ms() -> int:
    return int(time.time() * 1000)

def _cmp_window(points: List[Dict[str, Any]], hours: int) -> List[Dict[str, Any]]:
    if not points:
        return []
    cutoff = _cmp_now_ms() - hours * 60 * 60 * 1000
    return [p for p in points if int(p.get("t", 0)) >= cutoff]

def _cmp_chg_pct(points: List[Dict[str, Any]]) -> Optional[float]:
    if len(points) < 2:
        return None
    first = points[0].get("price")
    last = points[-1].get("price")
    if not first:
        return None
    try:
        return round(((last - first) / first) * 100.0, 2)
    except Exception:
        return None

def _cmp_low_high(points: List[Dict[str, Any]]) -> Dict[str, Optional[int]]:
    if not points:
        return {"low": None, "high": None}
    vals = [p.get("price") for p in points if isinstance(p.get("price"), (int, float))]
    return {"low": min(vals) if vals else None, "high": max(vals) if vals else None}

def _cmp_platform(p: str) -> str:
    p = (p or "").lower()
    if p in ("ps", "playstation", "console"):
        return "ps"
    if p in ("xbox", "xb"):
        return "xbox"
    if p in ("pc", "origin"):
        return "pc"
    return "ps"

async def _cmp_price_range_via_futgg(card_id: str) -> Dict[str, Optional[int]]:
    url = f"https://www.fut.gg/api/fut/player-item-definitions/26/{card_id}/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-GB,en;q=0.9",
        "Referer": "https://www.fut.gg/",
        "Origin": "https://www.fut.gg",
    }
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url, headers=headers, timeout=15) as r:
                if r.status != 200:
                    return {"min": None, "max": None}
                js = await r.json()
        data = js.get("data") or {}
        pr = data.get("priceRange") or {}
        mn = pr.get("min")
        mx = pr.get("max")
        if (mn is None or mx is None) and "ranges" in data:
            rng = (data["ranges"] or {}).get("priceRange") or {}
            mn = mn if mn is not None else rng.get("min")
            mx = mx if mx is not None else rng.get("max")
        return {"min": int(mn) if isinstance(mn, (int, float)) else None,
                "max": int(mx) if isinstance(mx, (int, float)) else None}
    except Exception:
        return {"min": None, "max": None}

async def _cmp_recent_sales_futbin(card_id: str, platform: str) -> List[Dict[str, Any]]:
    plat = platform if platform in ("ps", "xbox", "pc") else "ps"
    url = f"https://www.futbin.com/26/sales/{card_id}?platform={plat}"
    out: List[Dict[str, Any]] = []
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url, timeout=20, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-GB,en;q=0.9",
                "Referer": "https://www.futbin.com/",
            }) as r:
                if r.status != 200:
                    return out
                html = await r.text()
        m = re.search(r"var\s+table_data\s*=\s*(\[\s*{.*?}\s*\]);", html, re.S)
        if not m:
            return out
        raw = m.group(1)
        for price_str, when in re.findall(r"\{[^}]*price[^}\d]*(\d+)[^}]*time[^\"]*\"([^\"]+)\"[^}]*\}", raw):
            try:
                out.append({"price": int(price_str), "time": when})
            except Exception:
                continue
        return out[:20]
    except Exception:
        return []

@app.get("/api/player-compare")
async def player_compare(
    request: Request,
    ids: str = Query(..., description="CSV of 1 or 2 card_ids (as stored in fut_players)"),
    platform: Literal["ps","xbox","pc","console"] = Query("ps", description="ps|xbox|pc|console"),
    include_pc: bool = Query(True, description="Also return PC current price"),
    include_sales: bool = Query(True, description="Include recent sales list"),
):
    raw_ids = [x.strip() for x in ids.split(",") if x.strip()]
    if not raw_ids or len(raw_ids) > 2:
        raise HTTPException(status_code=400, detail="Provide 1 or 2 ids")
    plat = _cmp_platform(platform)

    async with request.app.state.player_pool.acquire() as pconn:
        meta_rows = await pconn.fetch("""
            SELECT card_id, name, rating, position, league, nation, club, image_url
            FROM fut_players
            WHERE card_id = ANY($1::text[])
        """, raw_ids)
    meta = {str(r["card_id"]): dict(r) for r in meta_rows}

    players_out: List[Dict[str, Any]] = []
    for cid_str in raw_ids:
        m = meta.get(cid_str, {})
        try:
            cid_int = int(cid_str)
        except Exception:
            cid_int = None

        # If you want to align with the unified fetcher, you could replace below with fetch_price(...).get("price")
        console_price = await get_player_price(cid_int, plat) if cid_int is not None else None
        pc_price = await get_player_price(cid_int, "pc") if (cid_int is not None and include_pc) else None

        hist_short = await get_price_history(cid_int, plat, "today") if cid_int is not None else []
        try:
            hist_long = await get_price_history(cid_int, plat, "week") if cid_int is not None else []
        except Exception:
            hist_long = hist_short

        w4 = _cmp_window(hist_short, 4)
        w24 = _cmp_window(hist_short, 24)
        chg4 = _cmp_chg_pct(w4)
        chg24 = _cmp_chg_pct(w24)
        lohi = _cmp_low_high(w24)

        pr = await _cmp_price_range_via_futgg(cid_str)
        sales = await _cmp_recent_sales_futbin(cid_str, plat) if include_sales else []

        players_out.append({
            "id": cid_str,
            "name": m.get("name") or f"Card {cid_str}",
            "rating": m.get("rating"),
            "position": m.get("position"),
            "league": m.get("league"),
            "nation": m.get("nation"),
            "club": m.get("club"),
            "image": m.get("image_url"),
            "prices": {"console": console_price, "pc": pc_price},
            "priceRange": pr,
            "trend": {
                "chg4hPct": chg4,
                "chg24hPct": chg24,
                "low24h": lohi["low"],
                "high24h": lohi["high"],
            },
            "history": {
                "short": hist_short,
                "long": hist_long,
            },
            "recentSales": sales,
        })

    return {"players": players_out}

@app.get("/api/events/next")
async def next_event(request: Request):
    async with request.app.state.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT name, kind, start_at, confidence FROM events WHERE start_at > $1 ORDER BY start_at ASC LIMIT 1",
            now_utc()
        )
    if row:
        return {"name": row["name"], "kind": row["kind"], "start_at": row["start_at"].isoformat(), "confidence": row["confidence"] or "heuristic"}
    nxt = next_daily_london_hour(18)
    return {"name": "Daily Content Drop", "kind": "promo", "start_at": nxt.isoformat(), "confidence": "heuristic"}

@app.post("/api/events")
async def create_event(request: Request, conn=Depends(get_db)):
    """Create a new event"""
    try:
        data = await request.json()

        # Validate required fields
        required_fields = ['name', 'kind', 'start_at']
        for field in required_fields:
            if not data.get(field):
                raise HTTPException(status_code=400, detail=f"Missing required field: {field}")

        # Parse the datetime
        try:
            start_at = datetime.fromisoformat(data['start_at'].replace('Z', '+00:00'))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid start_at format")

        # Optional end_at
        end_at = None
        if data.get('end_at'):
            try:
                end_at = datetime.fromisoformat(data['end_at'].replace('Z', '+00:00'))
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid end_at format")

        # Insert into database
        row = await conn.fetchrow("""
            INSERT INTO events (name, kind, start_at, end_at, confidence, source)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id, name, kind, start_at, end_at, confidence, source, created_at
        """,
            data['name'],
            data['kind'],
            start_at,
            end_at,
            data.get('confidence', 'heuristic'),
            data.get('source', 'manual_entry')
        )

        return {
            "id": row["id"],
            "name": row["name"],
            "kind": row["kind"],
            "start_at": row["start_at"].isoformat(),
            "end_at": row["end_at"].isoformat() if row["end_at"] else None,
            "confidence": row["confidence"],
            "source": row["source"],
            "created_at": row["created_at"].isoformat()
        }

    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Create event error: {e}")
        raise HTTPException(status_code=500, detail="Failed to create event")

@app.get("/api/deal-confidence/{card_id}")
async def deal_confidence(card_id: int, platform: str = "ps"):
    try:
        hist = await get_price_history(card_id, platform, "today")
    except Exception as e:
        raise HTTPException(502, f"history error: {e}")
    prices = [p.get("price") or p.get("v") or p.get("y") for p in hist if (p.get("price") or p.get("v") or p.get("y"))]
    if len(prices) < 6:
        live = await fetch_price(card_id, platform)
        if isinstance(live.get("price"), (int, float)):
            prices = [int(live["price"])] * 6
        else:
            return {"score": 0, "components": {}, "note": "no data"}

    n = len(prices)
    def _slope(xs):
        m = len(xs)
        if m < 2: return 0.0
        xb = (m-1)/2.0
        yb = sum(xs)/m
        num = sum((i-xb)*(y-yb) for i,y in enumerate(xs))
        den = sum((i-xb)**2 for i in range(m))
        return num/den if den else 0.0
    last_q = prices[max(0, n - max(6, n//4)):]
    sl = _slope(last_q)
    momentum4h = 1.0 if sl > 0 else 0.0
    first = prices[:n//2] or prices
    second = prices[n//2:] or prices
    regime = 1.0 if (sum(second)/len(second) >= sum(first)/len(first)) else 0.0
    diffs = [abs(prices[i]-prices[i-1]) for i in range(1, n)]
    vol_abs = sum(diffs)/len(diffs) if diffs else 0.0
    avgp = sum(prices)/len(prices)
    volRisk = min(1.0, (vol_abs/avgp) if avgp else 1.0)
    liquidity = min(1.0, max(0.0, (n-6)/90))
    wnd = prices[-min(12, n):]
    if wnd:
        lo, hi = min(wnd), max(wnd)
        spread_proxy = (hi-lo)/hi if hi else 0.1
    else:
        spread_proxy = 0.1
    recent_hi = max(wnd) if wnd else max(prices)
    cur = prices[-1]
    srRoom = (recent_hi - cur)/recent_hi if recent_hi else 0.0
    secs = (next_daily_london_hour(18) - now_utc()).total_seconds()
    catalyst = max(0.0, min(1.0, 1 - abs(secs)/(6*3600)))

    score = 100 * (0.22*momentum4h + 0.14*regime + 0.16*(1-volRisk) + 0.18*liquidity + 0.12*(1-spread_proxy) + 0.10*srRoom + 0.08*catalyst)
    score = max(0.0, min(100.0, score))
    return {"score": round(score,1), "components": {
        "momentum4h": round(momentum4h,3), "regimeAgreement": regime, "volRisk": round(volRisk,3),
        "liquidity": round(liquidity,3), "spreadProxy": round(spread_proxy,3), "srRoom": round(srRoom,3),
        "catalystBoost": round(catalyst,3)
    }}

@app.post("/api/backtest")
async def backtest(payload: Dict[str, Any]):
    players: List[int] = payload.get("players") or []
    platform: str = payload.get("platform", "ps")
    window_days: int = int(payload.get("window_days", 7))
    entry = payload.get("entry", {"type":"dip_from_high","x_pct":5})
    exit_ = payload.get("exit", {"tp_pct":7,"sl_pct":4,"max_hold_h":24})
    size = payload.get("size", {"coins":200000})
    concurrency = int(payload.get("concurrency", 3))

    if not players:
        raise HTTPException(400, "players required")

    def norm_series(hist: List[dict]) -> List[Tuple[int, float]]:
        out = []
        for p in hist:
            t = p.get("t") or p.get("ts") or p.get("time")
            v = p.get("price") or p.get("v") or p.get("y")
            if t is not None and v is not None:
                out.append((int(t), float(v)))
        return out

    equity = []; all_trades = []; cash = 0.0; open_trades = []
    for pid in players:
        hist = await get_price_history(pid, platform, "today")
        pts = norm_series(hist)[-(window_days*96):]
        if len(pts) < 16:
            continue
        recent_high = max(v for _,v in pts[:8])
        for i in range(8, len(pts)):
            t, px = pts[i]
            if px > recent_high: recent_high = px
            # exits
            keep = []
            for tr in open_trades:
                tp = tr["entry_price"]*(1+exit_.get("tp_pct",7)/100.0)
                sl = tr["entry_price"]*(1-exit_.get("sl_pct",4)/100.0)
                hold_h = (t - tr["t_in"]) / 3600000.0
                reason = None
                if px >= tp: reason="tp"
                elif px <= sl: reason="sl"
                elif hold_h >= exit_.get("max_hold_h",24): reason="time"
                if reason:
                    pnl = (px - tr["entry_price"]) * tr["qty"]
                    pnl_after_tax = pnl * 0.95
                    all_trades.append({**tr, "t_out": t, "px_out": px, "exit": reason, "pnl_after_tax": pnl_after_tax})
                    cash += pnl_after_tax
                else:
                    keep.append(tr)
            open_trades = keep
            # entries
            if len(open_trades) < concurrency and entry.get("type") == "dip_from_high":
                x = entry.get("x_pct", 5)
                if recent_high > 0 and ((recent_high - px)/recent_high)*100 >= x:
                    qty = max(1, int(size.get("coins",200000) // px))
                    open_trades.append({"player_id": pid, "t_in": t, "px_in": px, "entry_price": px, "qty": qty})
            equity.append({"t": t, "value": cash + sum((px - tr["entry_price"]) * tr["qty"] * 0.95 for tr in open_trades)})

    wins = [tr for tr in all_trades if tr["pnl_after_tax"] > 0]
    summary = {
        "trades": len(all_trades),
        "net_profit": round(sum(tr["pnl_after_tax"] for tr in all_trades), 2),
        "win_rate": round(100*len(wins)/len(all_trades), 1) if all_trades else 0.0,
        "avg_hold_h": round(sum(((tr["t_out"]-tr["t_in"])/3600000.0) for tr in all_trades)/len(all_trades), 2) if all_trades else 0.0,
    }
    return {"equity": equity, "summary": summary, "trades": all_trades}


# -------------- Billing & Account Endpoints --------------

@app.get("/api/billing/subscription")
async def get_subscription_status(user_id: str = Depends(get_current_user), conn=Depends(get_db)):
    """Get current subscription status"""
    try:
        subscription = await conn.fetchrow(
            """
            SELECT s.*, up.is_premium, up.premium_until
            FROM subscriptions s
            LEFT JOIN user_profiles up ON s.user_id = up.user_id
            WHERE s.user_id = $1 AND s.status = 'active'
            ORDER BY s.created_at DESC
            LIMIT 1
            """,
            user_id
        )
        if not subscription:
            return {"subscription": None}

        plan_id = subscription["plan_id"] or ""
        is_yearly = "year" in plan_id.lower()
        return {
            "subscription": {
                "plan_name": "Annual Premium" if is_yearly else "Monthly Premium",
                "next_billing_date": subscription["current_period_end"].isoformat() if subscription["current_period_end"] else None,
                "amount_display": "£99.99/year" if is_yearly else "£9.99/month",
                "status": subscription["status"],
                "cancel_at_period_end": subscription["cancel_at_period_end"]
            }
        }
    except Exception as e:
        logger.error(f"Get subscription error: {e}")
        raise HTTPException(status_code=500, detail="Failed to get subscription")

@app.post("/api/billing/create-checkout-session")
async def create_checkout_session(
    request: Request,
    user_id: str = Depends(get_current_user),
    conn=Depends(get_db)
):
    """Create Stripe checkout session"""
    try:
        data = await request.json()
        price_id = data.get("priceId")
        billing_cycle = data.get("billingCycle")
        # Map frontend price IDs to Stripe price IDs
        price_mapping = {
            "price_monthly_premium": os.getenv("STRIPE_MONTHLY_PRICE_ID"),
            "price_season_premium": os.getenv("STRIPE_SEASON_PRICE_ID")
        }
        stripe_price_id = price_mapping.get(price_id)
        if not stripe_price_id:
            raise HTTPException(status_code=400, detail="Invalid price ID")
        # Get user profile for email
        profile = await conn.fetchrow("SELECT * FROM user_profiles WHERE user_id = $1", user_id)
        if not profile:
            raise HTTPException(status_code=404, detail="User profile not found")
        # Create Stripe checkout session

        session = stripe.checkout.Session.create(
            customer_email=f"{profile['username']}@discord.local",  # Placeholder email
            payment_method_types=['card'],
            line_items=[{
                'price': stripe_price_id,
                'quantity': 1,
            }],
            mode='subscription',
            success_url=data.get("successUrl", os.getenv("BILLING_SUCCESS_URL")),
            cancel_url=data.get("cancelUrl", os.getenv("BILLING_CANCEL_URL")),
            metadata={
                'user_id': user_id,
                'billing_cycle': billing_cycle
            },
            subscription_data={
                'trial_period_days': data.get('trialDays', 7),
                'metadata': {
                    'user_id': user_id,
                    'discord_id': user_id
                }
            }
        )
        return {"sessionId": session.id, "checkoutUrl": session.url}
    except Exception as e:
        logger.error(f"Create checkout error: {e}")
        raise HTTPException(status_code=500, detail="Failed to create checkout session")

@app.post("/api/billing/cancel-subscription")
async def cancel_subscription(user_id: str = Depends(get_current_user), conn=Depends(get_db)):
    """Cancel user's subscription"""
    try:
        subscription = await conn.fetchrow(
            "SELECT * FROM subscriptions WHERE user_id = $1 AND status = 'active'",
            user_id
        )
        if not subscription:
            raise HTTPException(status_code=404, detail="No active subscription found")
        # Cancel in Stripe
        if subscription["stripe_subscription_id"]:
            stripe.Subscription.modify(
                subscription["stripe_subscription_id"],
                cancel_at_period_end=True
            )
        # Update database
        await conn.execute(
            "UPDATE subscriptions SET cancel_at_period_end = TRUE WHERE id = $1",
            subscription["id"]
        )
        return {"success": True}
    except Exception as e:
        logger.error(f"Cancel subscription error: {e}")
        raise HTTPException(status_code=500, detail="Failed to cancel subscription")

@app.post("/api/billing/update-payment-method")
async def update_payment_method(user_id: str = Depends(get_current_user), conn=Depends(get_db)):
    """Create customer portal session for payment method updates"""
    try:
        subscription = await conn.fetchrow(
            "SELECT * FROM subscriptions WHERE user_id = $1 AND status = 'active'",
            user_id
        )
        if not subscription or not subscription["stripe_customer_id"]:
            raise HTTPException(status_code=404, detail="No active subscription found")
        portal_session = stripe.billing_portal.Session.create(
            customer=subscription["stripe_customer_id"],
            return_url=f"{FRONTEND_URL}/billing"
        )
        return {"portalUrl": portal_session.url}
    except Exception as e:
        logger.error(f"Update payment method error: {e}")
        raise HTTPException(status_code=500, detail="Failed to update payment method")

# Webhook endpoint for Stripe events
@app.post("/api/webhooks/stripe")
async def stripe_webhook(request: Request, conn=Depends(get_db)):
    """Handle Stripe webhook events"""
    payload = await request.body()
    sig_header = request.headers.get('stripe-signature')
    webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET")
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, webhook_secret
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    try:
        if event['type'] == 'checkout.session.completed':
            await handle_checkout_completed(event['data']['object'], conn)
        elif event['type'] == 'customer.subscription.created':
            await handle_subscription_created(event['data']['object'], conn)
        elif event['type'] == 'customer.subscription.updated':
            await handle_subscription_updated(event['data']['object'], conn)
        elif event['type'] == 'customer.subscription.deleted':
            await handle_subscription_deleted(event['data']['object'], conn)
        elif event['type'] == 'invoice.payment_succeeded':
            await handle_payment_succeeded(event['data']['object'], conn)
        elif event['type'] == 'invoice.payment_failed':
            await handle_payment_failed(event['data']['object'], conn)
        return {"received": True}
    except Exception as e:
        logger.error(f"Webhook processing error: {e}")
        raise HTTPException(status_code=500, detail="Webhook processing failed")

# Webhook handler functions
async def handle_checkout_completed(session, conn):
    """Handle successful checkout"""
    user_id = session.get('metadata', {}).get('user_id')
    if user_id:
        # Assign Discord role immediately
        await discord_manager.assign_premium_role(user_id)

async def handle_subscription_created(subscription, conn):
    user_id = subscription['metadata'].get('user_id')
    if not user_id:
        return

    # Handle trial periods and missing dates
    current_period_start = None
    current_period_end = None

    # Try to get period dates, with fallbacks for trials
    if 'current_period_start' in subscription:
        current_period_start = datetime.fromtimestamp(subscription['current_period_start'], tz=timezone.utc)

    if 'current_period_end' in subscription:
        current_period_end = datetime.fromtimestamp(subscription['current_period_end'], tz=timezone.utc)
    elif 'trial_end' in subscription and subscription['trial_end']:
        # For trials, use trial_end as the period end
        current_period_end = datetime.fromtimestamp(subscription['trial_end'], tz=timezone.utc)

    # Save subscription to database
    await conn.execute(
        """
        INSERT INTO subscriptions (
            user_id, stripe_subscription_id, stripe_customer_id, status,
            plan_id, current_period_start, current_period_end
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        """,
        user_id,
        subscription['id'],
        subscription['customer'],
        subscription['status'],
        subscription['items']['data'][0]['price']['id'],
        current_period_start,
        current_period_end
    )
    # Update user profile
    await conn.execute(
        """
        UPDATE user_profiles SET is_premium = TRUE, premium_until = $2, updated_at = NOW()
        WHERE user_id = $1
        """,
        user_id,
        datetime.fromtimestamp(subscription.get('current_period_end'), tz=timezone.utc) if subscription.get('current_period_end') else None
    )
    # Assign Discord role and log
    await discord_manager.assign_premium_role(user_id)
    await conn.execute(
        """
        INSERT INTO discord_roles (user_id, discord_user_id, role_id, expires_at)
        VALUES ($1, $2, $3, $4)
        """,
        user_id,
        user_id,
        os.getenv("DISCORD_PREMIUM_ROLE_ID"),
        datetime.fromtimestamp(subscription.get('current_period_end'), tz=timezone.utc) if subscription.get('current_period_end') else None
    )

async def handle_subscription_updated(subscription, conn):
    """Handle subscription updates"""
    user_id = (subscription.get('metadata') or {}).get('user_id')
    if not user_id:
        return
    await conn.execute(
        """
        UPDATE subscriptions
           SET status = $2, current_period_end = $3, cancel_at_period_end = $4
         WHERE stripe_subscription_id = $1
        """,
        subscription.get('id'),
        subscription.get('status'),
        datetime.fromtimestamp(subscription.get('current_period_end'), tz=timezone.utc) if subscription.get('current_period_end') else None,
        subscription.get('cancel_at_period_end', False)
    )
    is_active = subscription.get('status') == 'active'
    await conn.execute(
        """
        UPDATE user_profiles
           SET is_premium = $2, premium_until = $3, updated_at = NOW()
         WHERE user_id = $1
        """,
        user_id,
        is_active,
        datetime.fromtimestamp(subscription.get('current_period_end'), tz=timezone.utc) if is_active and subscription.get('current_period_end') else None
    )

async def handle_subscription_deleted(subscription, conn):
    """Handle subscription cancellation"""
    user_id = (subscription.get('metadata') or {}).get('user_id')
    if not user_id:
        return
    await conn.execute(
        "UPDATE subscriptions SET status = 'canceled' WHERE stripe_subscription_id = $1",
        subscription.get('id')
    )
    await conn.execute(
        """
        UPDATE user_profiles
           SET is_premium = FALSE, premium_until = NULL, updated_at = NOW()
         WHERE user_id = $1
        """,
        user_id
    )
    await discord_manager.remove_premium_role(user_id)

async def handle_payment_succeeded(invoice, conn):
    """Handle successful payment"""
    subscription_id = invoice.get('subscription')
    if subscription_id:
        subscription = await conn.fetchrow(
            "SELECT user_id FROM subscriptions WHERE stripe_subscription_id = $1",
            subscription_id
        )
        if subscription:
            await conn.execute(
                """
                INSERT INTO payments (user_id, stripe_payment_intent_id, amount, currency, status)
                VALUES ($1, $2, $3, $4, $5)
                """,
                subscription['user_id'],
                invoice.get('payment_intent'),
                invoice.get('amount_paid'),
                (invoice.get('currency') or 'gbp').upper(),
                'succeeded'
            )

async def handle_payment_failed(invoice, conn):
    """Handle failed payment"""
    subscription_id = invoice.get('subscription')
    if subscription_id:
        subscription = await conn.fetchrow(
            "SELECT user_id FROM subscriptions WHERE stripe_subscription_id = $1",
            subscription_id
        )
        if subscription:
            await conn.execute(
                """
                INSERT INTO payments (user_id, stripe_payment_intent_id, amount, currency, status)
                VALUES ($1, $2, $3, $4, $5)
                """,
                subscription['user_id'],
                invoice.get('payment_intent'),
                invoice.get('amount_due'),
                (invoice.get('currency') or 'gbp').upper(),
                'failed'
            )

# Add endpoint to check Discord connection status
@app.get("/api/user/discord-status")
async def get_discord_status(user_id: str = Depends(get_current_user), conn=Depends(get_db)):
    """Check if user's Discord account is connected"""
    profile = await conn.fetchrow("SELECT username FROM user_profiles WHERE user_id = $1", user_id)
    return {"connected": bool(profile and profile["username"])}

try:
    from app.routers.squad import router as squad_router  # type: ignore
    app.include_router(squad_router, prefix="/api")
    logging.info("✅ Squad router loaded")
except Exception as e:
    logging.warning("⚠️ Squad router not loaded: %s", e)

# ---------- Candle aggregation loop ----------
ADVISORY_LOCK_KEY = 7741001  # prevents duplicate loops if you scale

@app.on_event("startup")
async def _bg_aggregator():
    async def loop():
        # Use the pool created during lifespan/startup
        pool = getattr(app.state, "pool", None)
        # give the app a moment to finish booting
        await asyncio.sleep(5)
        while True:
            try:
                if not pool:
                    logging.warning("Pool not initialized yet in aggregator loop.")
                else:
                    async with pool.acquire() as db:
                        got = await db.fetchval("SELECT pg_try_advisory_lock($1)", ADVISORY_LOCK_KEY)
                        if got:
                            try:
                                await aggregate_all_timeframes(db, since_hours=48)
                            finally:
                                await db.execute("SELECT pg_advisory_unlock($1)", ADVISORY_LOCK_KEY)
            except Exception as e:
                logging.error("aggregate error: %s", e)
            await asyncio.sleep(60)
    asyncio.create_task(loop())

# ---------- Global error handler (nicer 500s) ----------
@app.exception_handler(Exception)
async def _any_error(request: Request, exc: Exception):
    import traceback; traceback.print_exc()
    return JSONResponse(status_code=500, content={"path": str(request.url), "error": str(exc)})

# ---------- Tiny probes (remove later) ----------
_probe = APIRouter()

@_probe.get("/__whichdb")
async def whichdb(db=Depends(get_db)):
    return {
        "current_database": await db.fetchval("SELECT current_database()"),
        "current_schema": await db.fetchval("SELECT current_schema()"),
        "search_path": await db.fetchval("SHOW search_path"),
    }

@_probe.get("/__has_candles")
async def has_candles(player_card_id: str, platform: str = "ps", db=Depends(get_db)):
    cnt = await db.fetchval(
        "SELECT COUNT(*) FROM public.fut_candles WHERE player_card_id=$1 AND platform=$2",
        player_card_id, platform
    )
    return {"player_card_id": player_card_id, "platform": platform, "rows": cnt}

@app.get("/healthz")
def healthz():
    return {"ok": True}

app.include_router(_probe)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT)
