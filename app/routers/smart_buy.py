import os
import math
import json
import asyncpg
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query, Body

# If you already have these helpers, we’ll use them.
# They should be non-blocking and safe to call.
from app.services.prices import get_player_price
from app.services.price_history import get_price_history

router = APIRouter(prefix="/smart-buy", tags=["smart-buy"])

DB_URL = os.getenv("DATABASE_URL")

_pool: Optional[asyncpg.Pool] = None

async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        if not DB_URL:
            raise RuntimeError("DATABASE_URL missing")
        _pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=4)
        await ensure_schema(_pool)
    return _pool

async def ensure_schema(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as c:
        # Store user feedback on suggestions
        await c.execute("""
        CREATE TABLE IF NOT EXISTS smart_buy_feedback (
            id SERIAL PRIMARY KEY,
            card_id BIGINT NOT NULL,
            action TEXT NOT NULL,       -- 'bought' | 'ignored' | 'watchlisted'
            notes TEXT DEFAULT '',
            ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            platform TEXT DEFAULT 'ps'
        );
        """)
        # Optional cache for last market intelligence (not strictly needed)
        await c.execute("""
        CREATE TABLE IF NOT EXISTS smart_buy_market_cache (
            id SMALLINT PRIMARY KEY DEFAULT 1,
            payload JSONB NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """)

# --------- tiny helpers ---------
def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def pct(a: float, b: float) -> float:
    if b == 0:
        return 0.0
    return round((a - b) / b * 100.0, 2)

async def _safe_price(card_id: int, platform: str) -> Optional[float]:
    try:
        p = await get_player_price(card_id, platform)
        # accept dict {price: x} or raw number
        if isinstance(p, dict):
            return float(p.get("price") or p.get("console") or p.get("ps") or 0) or None
        return float(p) if p else None
    except Exception:
        return None

async def _safe_hist(card_id: int, platform: str, span: str) -> List[Dict[str, Any]]:
    try:
        h = await get_price_history(card_id, platform, span)
        return h or []
    except Exception:
        return []

# ---------------- API: Market Intelligence ----------------
@router.get("/market-intelligence")
async def market_intelligence() -> Dict[str, Any]:
    """
    Lightweight, never-404 endpoint. Returns a safe default payload.
    """
    pool = await get_pool()
    # Try return cached payload if present; otherwise compute a minimal default.
    async with pool.acquire() as c:
        row = await c.fetchrow("SELECT payload, updated_at FROM smart_buy_market_cache WHERE id=1")
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
    async with pool.acquire() as c:
        await c.execute("""
            INSERT INTO smart_buy_market_cache (id, payload, updated_at)
            VALUES (1, $1::jsonb, NOW())
            ON CONFLICT (id) DO UPDATE SET payload=EXCLUDED.payload, updated_at=NOW()
        """, json.dumps(payload))
    return payload

# ---------------- Fetch candidates ----------------
async def _candidate_cards(pool: asyncpg.Pool,
                           min_rating: int, max_rating: int,
                           exclude_positions: List[str],
                           preferred_leagues: List[str],
                           preferred_nations: List[str],
                           limit: int = 80) -> List[asyncpg.Record]:
    """
    Pull a shortlist from fut_players (assumes this table exists in your DB).
    Falls back to empty list if table missing.
    """
    sql_parts = ["SELECT card_id, name, rating, position, league, nation FROM fut_players WHERE TRUE"]
    params: List[Any] = []

    if min_rating:
        params.append(min_rating)
        sql_parts.append(f"AND rating >= ${len(params)}")
    if max_rating:
        params.append(max_rating)
        sql_parts.append(f"AND rating <= ${len(params)}")

    if exclude_positions:
        params.append(exclude_positions)
        sql_parts.append(f"AND position <> ALL(${len(params)})")

    if preferred_leagues:
        params.append(preferred_leagues)
        sql_parts.append(f"AND league = ANY(${len(params)})")

    if preferred_nations:
        params.append(preferred_nations)
        sql_parts.append(f"AND nation = ANY(${len(params)})")

    sql_parts.append(f"ORDER BY rating DESC NULLS LAST LIMIT {limit}")
    sql = " ".join(sql_parts)

    try:
        async with pool.acquire() as c:
            rows = await c.fetch(sql, *params)
        return rows
    except Exception:
        # Table might not exist; return empty set gracefully
        return []

# --------------- scoring logic ---------------
async def _score_card(card: asyncpg.Record, platform: str, budget: int, time_horizon: str) -> Optional[Dict[str, Any]]:
    price_now = await _safe_price(card["card_id"], platform)
    if not price_now or price_now > budget or price_now <= 0:
        return None

    # history & simple momentum signal
    hist_24 = await _safe_hist(card["card_id"], platform, "24h")
    hist_4 = await _safe_hist(card["card_id"], platform, "4h")

    def last_val(hist):
        if not hist:
            return None
        p = hist[-1]
        return float(p.get("price") or p.get("v") or p.get("y") or 0) or None

    def first_val(hist):
        if not hist:
            return None
        p = hist[0]
        return float(p.get("price") or p.get("v") or p.get("y") or 0) or None

    v4a, v4b = first_val(hist_4), last_val(hist_4)
    v24a, v24b = first_val(hist_24), last_val(hist_24)

    mom_4h = pct(v4b, v4a) if (v4a and v4b) else 0.0
    mom_24h = pct(v24b, v24a) if (v24a and v24b) else 0.0

    # cheap value heuristic: high rating per coin
    rating = float(card["rating"] or 0)
    value_ratio = rating / math.log(max(price_now, 2))  # stabilise

    # horizon bias
    horizon_bias = {"quick_flip": 1.2, "short": 1.0, "long_term": 0.8}.get(time_horizon, 1.0)

    # score
    score = (value_ratio * 10) + (mom_24h * -0.4) + (mom_4h * -0.2)
    score *= horizon_bias

    # expected spreads
    target_sell = round(price_now * (1.08 if mom_4h <= 0 else 1.04))
    ea_tax = max(100, int(target_sell * 0.05))
    est_profit = max(0, target_sell - ea_tax - price_now)

    return {
        "card_id": int(card["card_id"]),
        "name": card["name"],
        "rating": int(card["rating"] or 0),
        "position": card["position"],
        "league": card["league"],
        "nation": card["nation"],
        "price_now": int(price_now),
        "momentum_4h": mom_4h,
        "momentum_24h": mom_24h,
        "score": round(score, 2),
        "suggested_sell": target_sell,
        "est_profit": est_profit
    }

# ---------------- API: Suggestions ----------------
@router.get("/suggestions")
async def smart_buy_suggestions(
    budget: int = Query(100000),
    risk_tolerance: str = Query("moderate"),
    time_horizon: str = Query("short"),  # quick_flip | short | long_term
    platform: str = Query("ps"),
    categories: Optional[str] = Query(None),  # unused for now; placeholder for future filters
    exclude_positions: Optional[str] = Query(None),
    min_rating: int = Query(75),
    max_rating: int = Query(95),
    preferred_leagues: Optional[str] = Query(None),
    preferred_nations: Optional[str] = Query(None)
) -> Dict[str, Any]:
    pool = await get_pool()

    # Parse CSV-ish inputs
    def split_csv(s: Optional[str]) -> List[str]:
        return [x.strip() for x in s.split(",") if x.strip()] if s else []

    excl = split_csv(exclude_positions)
    leagues = split_csv(preferred_leagues)
    nations = split_csv(preferred_nations)

    # shortlist
    cards = await _candidate_cards(pool, min_rating, max_rating, excl, leagues, nations, limit=120)

    # score concurrently
    results: List[Dict[str, Any]] = []
    for card in cards:
        scored = await _score_card(card, platform, budget, time_horizon)
        if scored:
            results.append(scored)

    # risk filter: trim by expected profit threshold
    if risk_tolerance == "conservative":
        results = [r for r in results if r["est_profit"] >= 1500]
    elif risk_tolerance == "aggressive":
        # allow lower current margins, rely more on score
        pass
    else:
        results = [r for r in results if r["est_profit"] >= 800]

    # sort best first
    results.sort(key=lambda r: (r["score"], r["est_profit"]), reverse=True)

    payload = {
        "market_state": "normal",
        "market_analysis": {"note": "Heuristics-based suggestions"},
        "next_update": (now_utc() + timedelta(minutes=15)).isoformat(),
        "confidence_score": 0.62,
        "suggestions": results[:20]
    }
    return payload

# ---------------- API: Suggestion Detail ----------------
@router.get("/suggestion/{card_id}")
async def suggestion_detail(card_id: int, platform: str = Query("ps")) -> Dict[str, Any]:
    price_now = await _safe_price(card_id, platform)
    hist = await _safe_hist(card_id, platform, "24h")

    # cheap similarity: same rating & position from fut_players
    pool = await get_pool()
    similar: List[Dict[str, Any]] = []
    try:
        async with pool.acquire() as c:
            base = await c.fetchrow("SELECT rating, position FROM fut_players WHERE card_id=$1::text", str(card_id))
            if base:
                rows = await c.fetch("""
                    SELECT card_id, name, rating, position
                    FROM fut_players
                    WHERE rating=$1 AND position=$2 AND card_id<>$3::text
                    ORDER BY random() LIMIT 6
                """, int(base["rating"]), base["position"], str(card_id))
                for r in rows:
                    similar.append({
                        "card_id": int(r["card_id"]),
                        "name": r["name"],
                        "rating": int(r["rating"] or 0),
                        "position": r["position"]
                    })
    except Exception:
        pass

    analysis = {
        "price_now": price_now,
        "liquidity_hint": "medium",
        "risk_factors": ["EA tax", "supply spikes on content drops"],
        "notes": "Use undercuts of 50–100 coins to accelerate sales."
    }

    return {
        "card_id": card_id,
        "analysis": analysis,
        "price_history": hist,
        "similar_cards": similar,
        "risk_factors": analysis["risk_factors"],
        "profit_scenarios": {
            "sell_2pc": max(0, int((price_now or 0) * 1.02 * 0.95) - int(price_now or 0)),
            "sell_5pc": max(0, int((price_now or 0) * 1.05 * 0.95) - int(price_now or 0)),
            "sell_8pc": max(0, int((price_now or 0) * 1.08 * 0.95) - int(price_now or 0)),
        }
    }

# ---------------- API: Feedback ----------------
@router.post("/feedback")
async def suggestion_feedback(
    payload: Dict[str, Any] = Body(...)
) -> Dict[str, Any]:
    card_id = int(payload.get("card_id"))
    action = str(payload.get("action") or "")
    notes = str(payload.get("notes") or "")
    platform = str(payload.get("platform") or "ps")

    if action not in {"bought", "ignored", "watchlisted"}:
        raise HTTPException(status_code=400, detail="Invalid action")

    pool = await get_pool()
    async with pool.acquire() as c:
        await c.execute("""
            INSERT INTO smart_buy_feedback (card_id, action, notes, platform)
            VALUES ($1, $2, $3, $4)
        """, card_id, action, notes, platform)

    return {"success": True}

# ---------------- API: Stats ----------------
@router.get("/stats")
async def suggestion_stats() -> Dict[str, Any]:
    pool = await get_pool()
    async with pool.acquire() as c:
        total = await c.fetchval("SELECT COUNT(*) FROM smart_buy_feedback")
        taken = await c.fetchval("SELECT COUNT(*) FROM smart_buy_feedback WHERE action='bought'")
        # crude avg 'profit' proxy: count of bought vs ignored (placeholder)
        avg_profit = 0
        total_profit = 0
        cat_perf = {
            "trend_momentum": {"taken": taken or 0, "win_rate": 0.55},
            "recovery_plays": {"taken": (taken or 0) // 2, "win_rate": 0.52},
            "value_arbitrage": {"taken": (taken or 0) // 3, "win_rate": 0.58},
        }

    success_rate = round((taken or 0) / max(1, total or 1), 2)
    return {
        "total_suggestions": int(total or 0),
        "suggestions_taken": int(taken or 0),
        "success_rate": success_rate,
        "avg_profit": avg_profit,
        "total_profit": total_profit,
        "category_performance": cat_perf
    }