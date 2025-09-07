# app/routers/smart_buy.py
import os, math, json, asyncpg
from typing import Any, Dict, List, Optional
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, HTTPException, Query, Body

# reuse your existing services (fallbacks)
from app.services.prices import get_player_price
from app.services.price_history import get_price_history

router = APIRouter(prefix="/smart-buy", tags=["smart-buy"])

DB_URL = os.getenv("DATABASE_URL")
PLAYER_DB_URL = os.getenv("PLAYER_DATABASE_URL", DB_URL)

_main_pool: Optional[asyncpg.Pool] = None
_player_pool: Optional[asyncpg.Pool] = None

# ---------------- DB Pools ----------------
async def pool() -> asyncpg.Pool:
    global _main_pool
    if _main_pool is None:
        if not DB_URL:
            raise RuntimeError("DATABASE_URL missing")
        _main_pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=4)
        await _ensure_schema(_main_pool)
    return _main_pool

async def get_player_pool() -> asyncpg.Pool:
    global _player_pool
    if _player_pool is None:
        if not PLAYER_DB_URL:
            raise RuntimeError("PLAYER_DATABASE_URL missing")
        _player_pool = await asyncpg.create_pool(PLAYER_DB_URL, min_size=1, max_size=4)
    return _player_pool

async def _ensure_schema(p: asyncpg.Pool) -> None:
    async with p.acquire() as c:
        await c.execute("""
        CREATE TABLE IF NOT EXISTS smart_buy_feedback (
          id SERIAL PRIMARY KEY,
          card_id BIGINT NOT NULL,
          action TEXT NOT NULL,  -- bought|ignored|watchlisted
          notes TEXT DEFAULT '',
          platform TEXT DEFAULT 'ps',
          ts TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""")
        await c.execute("""
        CREATE TABLE IF NOT EXISTS smart_buy_market_cache (
          id SMALLINT PRIMARY KEY DEFAULT 1,
          payload JSONB NOT NULL,
          updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""")

# ---------------- Helpers ----------------
def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def _pct(new: Optional[float], old: Optional[float]) -> float:
    if not new or not old:
        return 0.0
    try:
        return round((new - old) / old * 100, 2)
    except ZeroDivisionError:
        return 0.0

def _split_csv(v: Optional[str]) -> List[str]:
    if not v:
        return []
    return [x.strip() for x in v.split(",") if x.strip()]

async def _safe_price(card_id: int, platform: str) -> Optional[float]:
    # Fallback live price fetcher (used only if DB price is missing)
    try:
        p = await get_player_price(card_id, platform)
        if isinstance(p, dict):
            p = p.get("price") or p.get("console") or p.get("ps")
        return float(p) if p else None
    except Exception:
        return None

async def _safe_hist(card_id: int, platform: str, span: str) -> List[Dict[str, Any]]:
    try:
        return await get_price_history(card_id, platform, span) or []
    except Exception:
        return []

# ---------------- Market Intelligence ----------------
@router.get("/market-intelligence")
async def market_intelligence() -> Dict[str, Any]:
    p = await pool()
    async with p.acquire() as c:
        row = await c.fetchrow("SELECT payload FROM smart_buy_market_cache WHERE id=1")
    if row:
        return row["payload"]
    payload = {
        "current_state": "normal",
        "upcoming_events": [],
        "crash_probability": 0.12,
        "recovery_indicators": {"breadth": 0.0, "volume": 0.0},
        "whale_activity": [],
        "meta_shifts": []
    }
    async with (await pool()).acquire() as c:
        await c.execute("""
        INSERT INTO smart_buy_market_cache (id, payload, updated_at)
        VALUES (1, $1::jsonb, NOW())
        ON CONFLICT (id) DO UPDATE SET payload=EXCLUDED.payload, updated_at=NOW()
        """, json.dumps(payload))
    return payload

# ---------------- Suggestions ----------------
async def _candidate_cards(
    min_rating: int,
    max_rating: int,
    exclude_positions: List[str],
    preferred_leagues: List[str],
    preferred_nations: List[str],
    budget: int,
    limit: int = 300
) -> List[asyncpg.Record]:
    """
    Pull candidates from fut_players using the numeric mirror column `price_num`.
    """
    player = await get_player_pool()

    sql = ["""
        SELECT
          card_id,
          name,
          rating,
          position,
          league,
          nation,
          price_num AS price
        FROM fut_players
        WHERE rating >= $1
          AND rating <= $2
          AND price_num IS NOT NULL
          AND price_num > 0
          AND price_num <= $3
    """]
    params: List[Any] = [min_rating, max_rating, budget]

    if exclude_positions:
        params.append(exclude_positions)
        sql.append(f"AND position <> ALL(${len(params)})")
    if preferred_leagues:
        params.append(preferred_leagues)
        sql.append(f"AND league = ANY(${len(params)})")
    if preferred_nations:
        params.append(preferred_nations)
        sql.append(f"AND nation = ANY(${len(params)})")

    sql.append("ORDER BY rating DESC NULLS LAST, price_num ASC NULLS LAST LIMIT {}".format(limit))

    try:
        async with player.acquire() as c:
            return await c.fetch(" ".join(sql), *params)
    except Exception:
        return []

async def _score(card: asyncpg.Record, platform: str, budget: int, horizon: str) -> Optional[Dict[str, Any]]:
    """
    Build a suggestion score. Prefer DB price (price_num) and only fall back to live price if needed.
    """
    price_now: Optional[float] = None
    db_price = card.get("price")
    if isinstance(db_price, (int, float)) and db_price > 0:
        price_now = float(db_price)
    else:
        price_now = await _safe_price(int(card["card_id"]), platform)

    if not price_now or price_now <= 0 or price_now > budget:
        return None

    # history for momentum
    h4 = await _safe_hist(int(card["card_id"]), platform, "4h")
    h24 = await _safe_hist(int(card["card_id"]), platform, "24h")

    def first_last(xs):
        if not xs:
            return (None, None)
        f = xs[0].get("price") or xs[0].get("v") or xs[0].get("y")
        l = xs[-1].get("price") or xs[-1].get("v") or xs[-1].get("y")
        return (float(f) if f else None, float(l) if l else None)

    f4, l4 = first_last(h4)
    f24, l24 = first_last(h24)
    mom4, mom24 = _pct(l4, f4), _pct(l24, f24)

    rating = float(card["rating"] or 0)
    value_ratio = rating / max(2.0, math.log(max(2.0, price_now)))
    bias = {"quick_flip": 1.2, "short": 1.0, "long_term": 0.8}.get(horizon, 1.0)

    score = ((value_ratio * 10) - 0.4 * (mom24 or 0) - 0.2 * (mom4 or 0)) * bias
    target = round(price_now * (1.08 if (mom4 or 0) <= 0 else 1.04))
    tax = int(target * 0.05)
    est = max(0, target - tax - int(price_now))

    return {
        "card_id": int(card["card_id"]),
        "name": card["name"],
        "rating": int(card["rating"] or 0),
        "position": card["position"],
        "league": card["league"],
        "nation": card["nation"],
        "price_now": int(price_now),
        "momentum_4h": mom4 or 0.0,
        "momentum_24h": mom24 or 0.0,
        "score": round(score, 2),
        "suggested_sell": target,
        "est_profit": est
    }

@router.get("/suggestions")
async def suggestions(
    budget: int = Query(100000),
    risk_tolerance: str = Query("moderate"),      # conservative | moderate | aggressive
    time_horizon: str = Query("short"),           # quick_flip | short | long_term
    platform: str = Query("ps"),
    categories: Optional[str] = Query(None),
    exclude_positions: Optional[str] = Query(None),
    min_rating: int = Query(75),
    max_rating: int = Query(95),
    preferred_leagues: Optional[str] = Query(None),
    preferred_nations: Optional[str] = Query(None),
) -> Dict[str, Any]:

    excl = _split_csv(exclude_positions)
    leagues = _split_csv(preferred_leagues)
    nations = _split_csv(preferred_nations)

    cards = await _candidate_cards(min_rating, max_rating, excl, leagues, nations, budget, limit=300)
    computed: List[Dict[str, Any]] = []
    for c in cards:
        s = await _score(c, platform, budget, time_horizon)
        if s:
            computed.append(s)

    # risk filter
    filtered = computed
    if risk_tolerance == "conservative":
        filtered = [r for r in computed if r["est_profit"] >= 1500]
    elif risk_tolerance == "moderate":
        filtered = [r for r in computed if r["est_profit"] >= 800]

    # fallback so UI isn't empty
    if not filtered and computed:
        filtered = sorted(computed, key=lambda r: (r["score"], r["est_profit"]), reverse=True)[:20]

    filtered.sort(key=lambda r: (r["score"], r["est_profit"]), reverse=True)

    return {
        "market_state": "normal",
        "market_analysis": {"note": "heuristics-based suggestions"},
        "next_update": (now_utc() + timedelta(minutes=15)).isoformat(),
        "confidence_score": 0.62,
        "suggestions": filtered[:20],
    }

# ---------------- Suggestion detail ----------------
@router.get("/suggestion/{card_id}")
async def suggestion_detail(card_id: int, platform: str = Query("ps")) -> Dict[str, Any]:
    # prefer price_num from DB; fallback to live
    price: Optional[float] = None
    try:
        async with (await get_player_pool()).acquire() as c:
            r = await c.fetchrow(
                "SELECT price_num FROM fut_players WHERE card_id=$1::text",
                str(card_id)
            )
        if r and r["price_num"] is not None:
            price = float(r["price_num"])
    except Exception:
        pass
    if price is None:
        price = await _safe_price(card_id, platform)

    hist = await _safe_hist(card_id, platform, "24h")

    # similar by rating+position
    sims: List[Dict[str, Any]] =
