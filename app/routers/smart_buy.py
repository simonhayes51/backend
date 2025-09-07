# app/routers/smart_buy.py
import os, math, json, asyncpg
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, HTTPException, Query, Body

# Reuse existing services
from app.services.prices import get_player_price
from app.services.price_history import get_price_history

router = APIRouter(prefix="/smart-buy", tags=["smart-buy"])

DB_URL = os.getenv("DATABASE_URL")
_pool: Optional[asyncpg.Pool] = None


# ---------- pool & schema ----------
async def pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        if not DB_URL:
            raise RuntimeError("DATABASE_URL missing")
        _pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=4)
        await _ensure_schema(_pool)
    return _pool


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


# ---------- helpers ----------
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _pct(new: Optional[float], old: Optional[float]) -> float:
    if new is None or old is None or old == 0:
        return 0.0
    try:
        return round((new - old) / old * 100.0, 2)
    except Exception:
        return 0.0


def _fl(xs: List[Dict[str, Any]]) -> Tuple[Optional[float], Optional[float]]:
    """extract first/last price from a history array with {price|v|y}"""
    if not xs:
        return (None, None)
    def _get(p):
        v = p.get("price")
        if v is None:
            v = p.get("v")
        if v is None:
            v = p.get("y")
        try:
            return float(v) if v is not None else None
        except Exception:
            return None
    return (_get(xs[0]), _get(xs[-1]))


async def _safe_price(card_id: int, platform: str) -> Optional[float]:
    """Return a positive float or None. Treat 0 / '0' as no price."""
    try:
        p = await get_player_price(card_id, platform)
        if isinstance(p, dict):
            p = p.get("price") or p.get("console") or p.get("ps")
        if p in (None, "", 0, "0", "0.0"):
            return None
        v = float(p)
        return v if v > 0 else None
    except Exception:
        return None


async def _safe_hist(card_id: int, platform: str, span: str) -> List[Dict[str, Any]]:
    try:
        data = await get_price_history(card_id, platform, span)
        return data or []
    except Exception:
        return []


# ---------- Market Intelligence (no-DB, never-500) ----------
@router.get("/market-intelligence")
async def market_intelligence() -> Dict[str, Any]:
    # Simple, safe default payload for the banner/cards
    return {
        "current_state": "normal",
        "upcoming_events": [],
        "crash_probability": 0.12,
        "recovery_indicators": {"breadth": 0.0, "volume": 0.0},
        "whale_activity": [],
        "meta_shifts": []
    }


# ---------- Suggestions ----------
async def _candidate_cards(min_rating: int, max_rating: int, budget: int, limit: int = 120) -> List[asyncpg.Record]:
    """
    Pulls only tradable cards:
      - price_num IS NOT NULL AND > 0
      - price_num <= budget (so we don't score things we can't buy)
    """
    try:
        async with (await pool()).acquire() as c:
            return await c.fetch("""
              SELECT card_id, name, rating, position, league, nation, price_num AS price
              FROM fut_players
              WHERE rating >= $1
                AND rating <= $2
                AND price_num IS NOT NULL
                AND price_num > 0
                AND price_num <= $3
              ORDER BY rating DESC NULLS LAST
              LIMIT $4
            """, min_rating, max_rating, budget, limit)
    except Exception:
        return []


async def _score(card: asyncpg.Record, platform: str, budget: int, horizon: str) -> Optional[Dict[str, Any]]:
    # Parse price from DB (price_num alias as price)
    raw_price = card.get("price")
    price_now: Optional[float] = None
    try:
        if raw_price is not None:
            price_now = float(raw_price)
    except (TypeError, ValueError):
        price_now = None

    # If missing from DB, try live
    if price_now is None or price_now <= 0:
        price_now = await _safe_price(int(card["card_id"]), platform)

    # Final guard: exclude untradables / budget overflow
    if price_now is None or price_now <= 0 or price_now > budget:
        return None

    # momentum windows
    h4 = await _safe_hist(int(card["card_id"]), platform, "4h")
    h24 = await _safe_hist(int(card["card_id"]), platform, "24h")
    f4, l4 = _fl(h4)
    f24, l24 = _fl(h24)
    mom4, mom24 = _pct(l4, f4), _pct(l24, f24)

    rating = float(card["rating"] or 0)
    # higher rating at same coin cost = better value
    value_ratio = rating / max(2.0, math.log(price_now))

    # horizon bias
    bias_map = {"quick_flip": 1.25, "short": 1.0, "long_term": 0.85}
    bias = bias_map.get(horizon, 1.0)

    # composite score – positive for cheap high-rated, penalise strong uptrends
    score = ((value_ratio * 10) - 0.4 * mom24 - 0.2 * mom4) * bias

    # conservative sell target (slightly higher if current momentum <= 0)
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

    cards = await _candidate_cards(min_rating, max_rating, budget, 120)

    out: List[Dict[str, Any]] = []
    for c in cards:
        s = await _score(c, platform, budget, time_horizon)
        if s:
            out.append(s)

    # risk filters
    if risk_tolerance == "conservative":
        out = [r for r in out if r["est_profit"] >= 1500]
    elif risk_tolerance == "moderate":
        out = [r for r in out if r["est_profit"] >= 800]
    # aggressive – no extra filter

    out.sort(key=lambda r: (r["score"], r["est_profit"]), reverse=True)

    return {
        "market_state": "normal",
        "market_analysis": {"note": "heuristics-based suggestions"},
        "next_update": (now_utc() + timedelta(minutes=15)).isoformat(),
        "confidence_score": 0.62,
        "suggestions": out[:20],
    }


# ---------- Suggestion detail ----------
@router.get("/suggestion/{card_id}")
async def suggestion_detail(card_id: int, platform: str = Query("ps")) -> Dict[str, Any]:
    price = await _safe_price(card_id, platform)
    hist = await _safe_hist(card_id, platform, "24h")

    # similar by rating+position
    sims: List[Dict[str, Any]] = []
    try:
        async with (await pool()).acquire() as c:
            base = await c.fetchrow(
                "SELECT rating, position FROM fut_players WHERE card_id=$1::text",
                str(card_id)
            )
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

    p = float(price or 0)
    def _p(x: float) -> int:
        return max(0, int(p * (1.0 + x) * 0.95) - int(p))

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
            "sell_2pc": _p(0.02),
            "sell_5pc": _p(0.05),
            "sell_8pc": _p(0.08),
        }
    }


# ---------- Feedback ----------
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


# ---------- Stats ----------
@router.get("/stats")
async def stats() -> Dict[str, Any]:
    try:
        async with (await pool()).acquire() as c:
            total = await c.fetchval("SELECT COUNT(*) FROM smart_buy_feedback")
            taken = await c.fetchval("SELECT COUNT(*) FROM smart_buy_feedback WHERE action='bought'")
    except Exception:
        total = taken = 0
    return {
        "total_suggestions": int(total or 0),
        "suggestions_taken": int(taken or 0),
        "success_rate": round((taken or 0)/max(1, total or 1), 2),
        "avg_profit": 0,
        "total_profit": 0,
        "category_performance": {}
    }
