# app/routers/smart_buy.py
import os, math, json, asyncpg
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, HTTPException, Query, Body

# Reuse existing services (kept as fallbacks)
from app.services.prices import get_player_price
from app.services.price_history import get_price_history

router = APIRouter(prefix="/smart-buy", tags=["smart-buy"])

DB_URL = os.getenv("PLAYER_DATABASE_URL") or os.getenv("DATABASE_URL")
if not DB_URL:
    raise RuntimeError("DATABASE_URL / PLAYER_DATABASE_URL missing")

_pool: Optional[asyncpg.Pool] = None
_cache_pool: Optional[asyncpg.Pool] = None  # may be the same as DB_URL

async def pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=6)
        await _ensure_schema(_pool)
    return _pool

async def cache_pool() -> asyncpg.Pool:
    # use the same DB unless you purposely separate
    global _cache_pool
    if _cache_pool is None:
        _cache_pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=2)
        await _ensure_schema(_cache_pool)
    return _cache_pool

async def _ensure_schema(p: asyncpg.Pool) -> None:
    async with p.acquire() as c:
        await c.execute("""
        CREATE TABLE IF NOT EXISTS smart_buy_feedback (
          id SERIAL PRIMARY KEY,
          card_id BIGINT NOT NULL,
          action TEXT NOT NULL,  -- bought|ignored|watchlisted
          notes  TEXT DEFAULT '',
          platform TEXT DEFAULT 'ps',
          ts TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""")
        await c.execute("""
        CREATE TABLE IF NOT EXISTS smart_buy_market_cache (
          id SMALLINT PRIMARY KEY DEFAULT 1,
          payload JSONB NOT NULL,
          updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""")

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def _pct(new: Optional[float], old: Optional[float]) -> float:
    try:
        if not new or not old:
            return 0.0
        return round((new - old) / old * 100.0, 2)
    except Exception:
        return 0.0

# ---------- Pricing helpers ----------
async def _db_price(card_id: int) -> Tuple[Optional[int], Optional[datetime]]:
    """
    Fast path: read price_num & price_updated_at from fut_players.
    """
    try:
        async with (await pool()).acquire() as c:
            r = await c.fetchrow(
                "SELECT price_num, price_updated_at FROM fut_players WHERE card_id=$1::text",
                str(card_id),
            )
        if not r:
            return None, None
        pn = r["price_num"]
        if isinstance(pn, int) and pn > 0:
            return pn, r["price_updated_at"]
        return None, r["price_updated_at"]
    except Exception:
        return None, None

async def _safe_price(card_id: int, platform: str) -> Optional[int]:
    """
    Prefer DB price; fallback to upstream service if missing.
    """
    pn, _ = await _db_price(card_id)
    if isinstance(pn, int) and pn > 0:
        return pn
    try:
        p = await get_player_price(card_id, platform)
        if isinstance(p, dict):
            # support a couple of shapes
            p = p.get("price") or p.get("console") or p.get("ps")
        return int(p) if p else None
    except Exception:
        return None

async def _safe_hist(card_id: int, platform: str, span: str) -> List[Dict[str, Any]]:
    try:
        h = await get_price_history(card_id, platform, span)
        return h or []
    except Exception:
        return []

def _first_last(xs: List[Dict[str, Any]]) -> Tuple[Optional[float], Optional[float]]:
    """
    Robustly extract first/last price from heterogeneous history points.
    """
    if not xs:
        return None, None
    def _val(p):
        return p.get("price") or p.get("v") or p.get("y")
    f = _val(xs[0]); l = _val(xs[-1])
    try:
        return (float(f) if f is not None else None,
                float(l) if l is not None else None)
    except Exception:
        return None, None

# ---------- Market Intelligence ----------
@router.get("/market-intelligence")
async def market_intelligence() -> Dict[str, Any]:
    cp = await cache_pool()
    async with cp.acquire() as c:
        row = await c.fetchrow("SELECT payload FROM smart_buy_market_cache WHERE id=1")
    if row:
        return row["payload"]

    # default payload (will be upserted)
    payload = {
        "current_state": "normal",
        "upcoming_events": [],
        "crash_probability": 0.10,
        "recovery_indicators": {"breadth": 0.0, "volume": 0.0},
        "whale_activity": [],
        "meta_shifts": []
    }
    async with cp.acquire() as c:
        await c.execute("""
            INSERT INTO smart_buy_market_cache (id, payload, updated_at)
            VALUES (1, $1::jsonb, NOW())
            ON CONFLICT (id) DO UPDATE SET payload=EXCLUDED.payload, updated_at=NOW()
        """, json.dumps(payload))
    return payload

# ---------- Candidate cards ----------
async def _candidate_cards(
    min_rating: int,
    max_rating: int,
    budget: int,
    limit: int = 200,
) -> List[asyncpg.Record]:
    """
    Pull candidates directly from fut_players using price_num.
    Excludes untradeables (price_num=0) and respects budget.
    """
    try:
        async with (await pool()).acquire() as c:
            rows = await c.fetch("""
              SELECT
                card_id, name, rating, position, league, nation, price_num
              FROM fut_players
              WHERE rating >= $1
                AND rating <= $2
                AND price_num IS NOT NULL
                AND price_num > 0
                AND price_num <= $3
              ORDER BY rating DESC NULLS LAST, price_num DESC
              LIMIT $4
            """, min_rating, max_rating, budget, limit)
        if rows:
            return rows

        # Fallback: if nothing under budget, bring *some* tradable cards so UI shows results,
        # then we’ll filter by budget inside _score (still respecting it).
        async with (await pool()).acquire() as c:
            rows = await c.fetch("""
              SELECT card_id, name, rating, position, league, nation, price_num
              FROM fut_players
              WHERE rating >= $1
                AND rating <= $2
                AND price_num IS NOT NULL
                AND price_num > 0
              ORDER BY rating DESC NULLS LAST, price_num ASC
              LIMIT $3
            """, min_rating, max_rating, limit)
        return rows
    except Exception:
        return []

# ---------- Scoring ----------
async def _score(
    card: asyncpg.Record,
    platform: str,
    budget: int,
    horizon: str
) -> Optional[Dict[str, Any]]:
    """
    Compute score; gracefully handle thin/no history.
    """
    # Prefer DB price if present in row
    row_price = card.get("price_num")
    price_now = int(row_price) if isinstance(row_price, int) and row_price > 0 else None

    if price_now is None:
        price_now = await _safe_price(int(card["card_id"]), platform)

    if not price_now or price_now <= 0:
        return None

    # Respect budget (even if fallback brought pricier cards)
    if price_now > budget:
        return None

    # Try histories; momentum defaults to 0 if missing
    h4 = await _safe_hist(int(card["card_id"]), platform, "4h")
    h24 = await _safe_hist(int(card["card_id"]), platform, "24h")
    f4,  l4  = _first_last(h4)
    f24, l24 = _first_last(h24)
    mom4  = _pct(l4 or price_now,  f4  or price_now)
    mom24 = _pct(l24 or price_now, f24 or price_now)

    rating = float(card["rating"] or 0)
    # simple value metric: higher rating + lower price
    value_ratio = rating / max(2.0, math.log(price_now))
    bias = {"quick_flip": 1.2, "short": 1.0, "long_term": 0.85}.get(horizon, 1.0)

    score = ((value_ratio * 10) - 0.35 * mom24 - 0.15 * mom4) * bias

    # target sell: if short-term momentum flat/negative, assume small mean-revert;
    # else be conservative
    uplift = 1.08 if mom4 <= 0 else 1.04
    target = int(round(price_now * uplift))
    est_tax = int(round(target * 0.05))
    est_profit = max(0, target - est_tax - int(price_now))

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
        "est_profit": int(est_profit)
    }

# ---------- Suggestions ----------
@router.get("/suggestions")
async def suggestions(
    budget: int = Query(100000),
    risk_tolerance: str = Query("moderate"),        # conservative | moderate | aggressive
    time_horizon: str = Query("short"),             # quick_flip | short | long_term
    platform: str = Query("ps"),
    categories: Optional[str] = Query(None),
    exclude_positions: Optional[str] = Query(None),
    min_rating: int = Query(75),
    max_rating: int = Query(99),
    preferred_leagues: Optional[str] = Query(None),
    preferred_nations: Optional[str] = Query(None),
) -> Dict[str, Any]:

    cards = await _candidate_cards(min_rating, max_rating, budget, 240)
    out: List[Dict[str, Any]] = []

    for c in cards:
        # optional position / league / nation filtering
        if exclude_positions:
            ex = {x.strip().upper() for x in exclude_positions.split(",") if x.strip()}
            pos = (c.get("position") or "").upper()
            if pos in ex:
                continue
        if preferred_leagues:
            pls = {x.strip() for x in preferred_leagues.split(",") if x.strip()}
            if pls and (c.get("league") not in pls):
                continue
        if preferred_nations:
            pns = {x.strip() for x in preferred_nations.split(",") if x.strip()}
            if pns and (c.get("nation") not in pns):
                continue

        s = await _score(c, platform, budget, time_horizon)
        if s:
            out.append(s)

    # Risk gating — loosened to ensure results while testing
    if risk_tolerance == "conservative":
        out = [r for r in out if r["est_profit"] >= 500]
    elif risk_tolerance == "moderate":
        out = [r for r in out if r["est_profit"] >= 200]
    else:  # aggressive
        # keep all non-negative
        out = [r for r in out if r["est_profit"] >= 0]

    out.sort(key=lambda r: (r["score"], r["est_profit"]), reverse=True)

    return {
        "market_state": "normal",
        "market_analysis": {"note": "heuristics-based suggestions"},
        "next_update": (now_utc() + timedelta(minutes=15)).isoformat(),
        "confidence_score": 0.65,
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
            base = await c.fetchrow("SELECT rating, position FROM fut_players WHERE card_id=$1::text", str(card_id))
            if base:
                rows = await c.fetch("""
                  SELECT card_id, name, rating, position
                  FROM fut_players
                  WHERE rating=$1 AND position=$2 AND card_id<>$3::text
                    AND price_num IS NOT NULL AND price_num > 0
                  ORDER BY random() LIMIT 6
                """, int(base["rating"]), base["position"], str(card_id))
                for r in rows:
                    sims.append({
                        "card_id": int(r["card_id"]), "name": r["name"],
                        "rating": int(r["rating"] or 0), "position": r["position"]
                    })
    except Exception:
        pass

    def _p(pct: float) -> int:
        if not isinstance(price, (int, float)) or price <= 0:
            return 0
        gross = int(round(price * (1 + pct/100.0)))
        return max(0, int(gross * 0.95) - int(price))

    return {
        "card_id": card_id,
        "analysis": {
            "price_now": price,
            "liquidity_hint": "medium",
            "risk_factors": ["EA tax", "supply spikes on content drops"],
            "notes": "Use 50–100c undercuts to accelerate sales."
        },
        "price_history": hist,
        "similar_cards": sims,
        "risk_factors": ["EA tax", "supply spikes on content drops"],
        "profit_scenarios": {
            "sell_2pc": _p(2),
            "sell_5pc": _p(5),
            "sell_8pc": _p(8),
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
    async with (await cache_pool()).acquire() as c:
        await c.execute(
            "INSERT INTO smart_buy_feedback (card_id, action, notes, platform) VALUES ($1,$2,$3,$4)",
            card_id, action, notes, platform
        )
    return {"success": True}

# ---------- Stats ----------
@router.get("/stats")
async def stats() -> Dict[str, Any]:
    async with (await cache_pool()).acquire() as c:
        total = await c.fetchval("SELECT COUNT(*) FROM smart_buy_feedback")
        taken = await c.fetchval("SELECT COUNT(*) FROM smart_buy_feedback WHERE action='bought'")
    return {
        "total_suggestions": int(total or 0),
        "suggestions_taken": int(taken or 0),
        "success_rate": round((taken or 0)/max(1, total or 1), 2),
        "avg_profit": 0,
        "total_profit": 0,
        "category_performance": {}
    }
