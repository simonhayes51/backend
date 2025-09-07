# app/routers/smart_buy.py
import os, math, json, asyncpg
from typing import Any, Dict, List, Optional
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, HTTPException, Query, Body

# Reuse service helpers already in your app
from app.services.prices import get_player_price
from app.services.price_history import get_price_history

router = APIRouter(prefix="/smart-buy", tags=["smart-buy"])

# ---------- ENV / POOLS ----------
DB_URL = os.getenv("DATABASE_URL")
PLAYER_DB_URL = os.getenv("PLAYER_DATABASE_URL", DB_URL)

_main_pool: Optional[asyncpg.Pool] = None
_player_pool: Optional[asyncpg.Pool] = None


async def pool() -> asyncpg.Pool:
    """Main app DB (cache + feedback, *not* fut_players)."""
    global _main_pool
    if _main_pool is None:
        if not DB_URL:
            raise RuntimeError("DATABASE_URL missing")
        _main_pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=4)
        await _ensure_schema(_main_pool)
    return _main_pool


async def get_player_pool() -> asyncpg.Pool:
    """Players DB where fut_players lives."""
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

# ---------- HELPERS ----------
def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def _pct(new: Optional[float], old: Optional[float]) -> float:
    if not new or not old:
        return 0.0
    try:
        return round((new - old) / old * 100.0, 2)
    except ZeroDivisionError:
        return 0.0

async def _safe_price(card_id: int, platform: str) -> Optional[float]:
    """Best-effort live price from your service."""
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

def _split_csv(v: Optional[str]) -> List[str]:
    return [x.strip() for x in (v or "").split(",") if x and x.strip()]

# ---- robust history parsing (prevents 500s with mixed formats) ----
def _extract_prices(xs) -> List[float]:
    out: List[float] = []
    for p in xs or []:
        if isinstance(p, (int, float)):
            out.append(float(p)); continue
        if isinstance(p, dict):
            v = p.get("price")
            if v is None: v = p.get("v")
            if v is None: v = p.get("y")
            if isinstance(v, (int, float)): out.append(float(v))
            continue
        if isinstance(p, (list, tuple)) and p:
            cand = p[-1]
            if isinstance(cand, (int, float)): out.append(float(cand))
    return out

def _first_last_pct(xs) -> float:
    vals = _extract_prices(xs)
    if len(vals) < 2: return 0.0
    first, last = vals[0], vals[-1]
    if not first: return 0.0
    try:
        return round((last - first) / first * 100.0, 2)
    except ZeroDivisionError:
        return 0.0

# ---------- MARKET INTELLIGENCE ----------
@router.get("/market-intelligence")
async def market_intelligence() -> Dict[str, Any]:
    """Always returns 200; falls back to static payload if cache/DB fails."""
    fallback = {
        "current_state": "normal",
        "upcoming_events": [],
        "crash_probability": 0.12,
        "recovery_indicators": {"breadth": 0.0, "volume": 0.0},
        "whale_activity": [],
        "meta_shifts": []
    }
    try:
        p = await pool()
        async with p.acquire() as c:
            row = await c.fetchrow("SELECT payload FROM smart_buy_market_cache WHERE id=1")
        if row and row["payload"]:
            return row["payload"]
        # seed cache
        async with (await pool()).acquire() as c:
            await c.execute("""
                INSERT INTO smart_buy_market_cache (id, payload, updated_at)
                VALUES (1, $1::jsonb, NOW())
                ON CONFLICT (id) DO UPDATE
                  SET payload=EXCLUDED.payload, updated_at=NOW()
            """, json.dumps(fallback))
        return fallback
    except Exception:
        return fallback

# ---------- SUGGESTIONS ----------
async def _candidate_cards(
    min_rating: int,
    max_rating: int,
    exclude_positions: List[str],
    preferred_leagues: List[str],
    preferred_nations: List[str],
    budget: int,
    limit: int = 300,
) -> List[asyncpg.Record]:
    """
    Pull candidates from fut_players using price_num (numeric mirror of price).
    """
    player = await get_player_pool()
    sql = ["""
        SELECT
          card_id, name, rating, position, league, nation,
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

    sql.append("ORDER BY rating DESC NULLS LAST, price_num ASC NULLS LAST")
    sql.append(f"LIMIT {int(limit)}")

    try:
        async with player.acquire() as c:
            return await c.fetch(" ".join(sql), *params)
    except Exception:
        return []

async def _score(card: asyncpg.Record, platform: str, budget: int, horizon: str) -> Optional[Dict[str, Any]]:
    """Score each candidate; prefer DB price; fallback to live price."""
    db_price = card.get("price")
    price_now: Optional[float] = float(db_price) if isinstance(db_price, (int, float)) and db_price > 0 else None
    if price_now is None:
        price_now = await _safe_price(int(card["card_id"]), platform)

    if not price_now or price_now <= 0 or price_now > budget:
        return None

    # momentum
    h4  = await _safe_hist(int(card["card_id"]), platform, "4h")
    h24 = await _safe_hist(int(card["card_id"]), platform, "24h")
    mom4  = _first_last_pct(h4)
    mom24 = _first_last_pct(h24)

    rating = float(card["rating"] or 0)
    value_ratio = rating / max(2.0, math.log(max(2.0, price_now)))
    bias = {"quick_flip": 1.2, "short": 1.0, "long_term": 0.8}.get(horizon, 1.0)

    score = ((value_ratio * 10) - 0.4 * mom24 - 0.2 * mom4) * bias
    target = round(price_now * (1.08 if mom4 <= 0 else 1.04))
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
        "momentum_4h": mom4,
        "momentum_24h": mom24,
        "score": round(score, 2),
        "suggested_sell": target,
        "est_profit": est
    }

@router.get("/suggestions")
async def suggestions(
    budget: int = Query(100000),
    risk_tolerance: str = Query("moderate"),
    time_horizon: str = Query("short"),
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
    results: List[Dict[str, Any]] = []
    for c in cards:
        s = await _score(c, platform, budget, time_horizon)
        if s:
            results.append(s)

    # risk filters
    filtered = results
    if risk_tolerance == "conservative":
        filtered = [r for r in results if r["est_profit"] >= 1500]
    elif risk_tolerance == "moderate":
        filtered = [r for r in results if r["est_profit"] >= 800]

    if not filtered and results:
        filtered = sorted(results, key=lambda r: (r["score"], r["est_profit"]), reverse=True)[:20]

    filtered.sort(key=lambda r: (r["score"], r["est_profit"]), reverse=True)

    return {
        "market_state": "normal",
        "market_analysis": {"note": "heuristics-based suggestions"},
        "next_update": (now_utc() + timedelta(minutes=15)).isoformat(),
        "confidence_score": 0.62,
        "suggestions": filtered[:20],
    }

# ---------- SUGGESTION DETAIL ----------
@router.get("/suggestion/{card_id}")
async def suggestion_detail(card_id: int, platform: str = Query("ps")) -> Dict[str, Any]:
    # Prefer DB numeric price
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

    # Similar by rating+position
    sims: List[Dict[str, Any]] = []
    try:
        async with (await get_player_pool()).acquire() as c:
            base = await c.fetchrow("SELECT rating, position FROM fut_players WHERE card_id=$1::text", str(card_id))
            if base:
                rows = await c.fetch("""
                  SELECT card_id, name, rating, position
                  FROM fut_players
                  WHERE rating=$1 AND position=$2 AND card_id<>$3::text
                  ORDER BY random() LIMIT 6
                """, int(base["rating"]), base["position"], str(card_id))
                for r in rows:
                    sims.append({
                        "card_id": int(r["card_id"]),
                        "name": r["name"],
                        "rating": int(r["rating"] or 0),
                        "position": r["position"]
                    })
    except Exception:
        pass

    return {
        "card_id": card_id,
        "analysis": {
            "price_now": price,
            "liquidity_hint": "medium",
            "risk_factors": ["EA tax", "supply spikes on content drops"],
            "notes": "Use 50–100 coin undercuts to accelerate sales."
        },
        "price_history": hist,
        "similar_cards": sims,
        "risk_factors": ["EA tax", "supply spikes on content drops"],
        "profit_scenarios": {
            "sell_2pc": max(0, int((price or 0) * 1.02 * 0.95) - int(price or 0)),
            "sell_5pc": max(0, int((price or 0) * 1.05 * 0.95) - int(price or 0)),
            "sell_8pc": max(0, int((price or 0) * 1.08 * 0.95) - int(price or 0)),
        }
    }

# ---------- FEEDBACK ----------
@router.post("/feedback")
async def feedback(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    card_id = int(payload.get("card_id"))
    action = str(payload.get("action") or "")
    notes = str(payload.get("notes") or "")
    platform = str(payload.get("platform") or "ps")
    if action not in {"bought", "ignored", "watchlisted"}:
        raise HTTPException(400, "Invalid action")
    async with (await pool()).acquire() as c:
        await c.execute(
            "INSERT INTO smart_buy_feedback (card_id, action, notes, platform) VALUES ($1,$2,$3,$4)",
            card_id, action, notes, platform
        )
    return {"success": True}

# ---------- STATS ----------
@router.get("/stats")
async def stats() -> Dict[str, Any]:
    async with (await pool()).acquire() as c:
        total = await c.fetchval("SELECT COUNT(*) FROM smart_buy_feedback")
        taken = await c.fetchval("SELECT COUNT(*) FROM smart_buy_feedback WHERE action='bought'")
    return {
        "total_suggestions": int(total or 0),
        "suggestions_taken": int(taken or 0),
        "success_rate": round((taken or 0) / max(1, total or 1), 2),
        "avg_profit": 0,
        "total_profit": 0,
        "category_performance": {}
    }

# ---------- DEBUG (remove later) ----------
@router.get("/_debug")
async def smart_buy_debug() -> Dict[str, Any]:
    info: Dict[str, Any] = {
        "env_seen": {
            "DATABASE_URL_set": bool(DB_URL),
            "PLAYER_DATABASE_URL_set": bool(PLAYER_DB_URL),
        }
    }
    # main DB
    try:
        p = await pool()
        async with p.acquire() as c:
            await c.fetchval("SELECT 1")
        info["main_db_ok"] = True
    except Exception as e:
        info["main_db_ok"] = False
        info["main_db_error"] = str(e)

    # players DB / table / column counts
    try:
        pp = await get_player_pool()
        async with pp.acquire() as c:
            exists = await c.fetchval("SELECT to_regclass('public.fut_players') IS NOT NULL")
            info["players_table_exists"] = bool(exists)
            has_price_num = await c.fetchval("""
                SELECT EXISTS (
                  SELECT 1
                  FROM information_schema.columns
                  WHERE table_schema='public'
                    AND table_name='fut_players'
                    AND column_name='price_num'
                )
            """)
            info["has_price_num"] = bool(has_price_num)
            if exists:
                info["players_rowcount"] = int(await c.fetchval("SELECT COUNT(*) FROM fut_players") or 0)
            if has_price_num:
                info["price_num_non_null"] = int(await c.fetchval(
                    "SELECT COUNT(*) FROM fut_players WHERE price_num IS NOT NULL"
                ) or 0)
    except Exception as e:
        info["player_db_error"] = str(e)

    return info
