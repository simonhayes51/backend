# app/routers/smart_buy.py
from __future__ import annotations

import math
import random
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

router = APIRouter(prefix="/smart-buy", tags=["smart-buy"])

# ------------------------------
# Helpers (no imports from main)
# ------------------------------

def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _get_pools(request: Request) -> tuple[asyncpg.pool.Pool, asyncpg.pool.Pool]:
    pool = getattr(request.app.state, "pool", None)
    player_pool = getattr(request.app.state, "player_pool", None) or pool
    if pool is None:
        raise HTTPException(500, "Database pool not ready")
    return pool, player_pool

def _get_current_user(request: Request) -> str:
    uid = request.session.get("user_id")
    if not uid:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return uid

def _norm_platform(p: str) -> str:
    p = (p or "").lower()
    if p in ("ps", "playstation", "console"):
        return "ps"
    if p in ("xb", "xbox"):
        return "xbox"
    if p in ("pc", "origin"):
        return "pc"
    return "ps"

# ---------------------------------
# Request/Response data structures
# ---------------------------------

RiskTolerance = Literal["low", "moderate", "high"]
TimeHorizon = Literal["short", "medium", "long"]

class FeedbackIn(BaseModel):
    card_id: str
    action: Literal["taken", "dismissed", "watching"]
    notes: Optional[str] = None
    actual_buy_price: Optional[int] = None
    actual_sell_price: Optional[int] = None

# ------------------------------
# Market intelligence endpoint
# ------------------------------

@router.get("/market-intelligence")
async def market_intelligence(request: Request) -> Dict[str, Any]:
    """
    Returns the latest detected market state if available; otherwise a benign default.
    Works even if the table is empty.
    """
    pool, _ = _get_pools(request)
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT platform, state, confidence_score, detected_at, indicators
                FROM market_states
                ORDER BY detected_at DESC
                LIMIT 1
                """
            )
    except Exception:
        row = None

    if not row:
        return {
            "state": "normal",
            "confidence": 0,
            "platform": "all",
            "detected_at": _now_utc_iso(),
            "indicators": {},
        }

    return {
        "state": row["state"],
        "confidence": int(row["confidence_score"] or 0),
        "platform": row["platform"],
        "detected_at": (row["detected_at"].astimezone(timezone.utc).isoformat()
                        if row["detected_at"] else _now_utc_iso()),
        "indicators": row.get("indicators") or {},
    }

# ------------------------------
# Suggestions endpoint
# ------------------------------

@router.get("/suggestions")
async def get_suggestions(
    request: Request,
    budget: int = Query(100_000, ge=500),
    risk_tolerance: RiskTolerance = Query("moderate"),
    time_horizon: TimeHorizon = Query("short"),
    platform: str = Query("ps"),
    limit: int = Query(12, ge=1, le=50),
) -> Dict[str, Any]:
    """
    Returns suggestions from smart_buy_suggestions if present;
    falls back to synthesizing picks from fut_players.price (text → int).
    Enriches with fut_players metadata. All joins are TEXT on card_id.
    """
    _, player_pool = _get_pools(request)
    plat = _norm_platform(platform)

    # Multipliers by risk & horizon
    risk_pct = {"low": (0.05, 0.08), "moderate": (0.08, 0.12), "high": (0.12, 0.18)}[risk_tolerance]
    horizon_bonus = {"short": 0.0, "medium": 0.02, "long": 0.04}[time_horizon]

    # 1) Try to read precomputed suggestions (same DB as fut_players OR trades DB if you prefer)
    # We only take ones within budget & platform. card_id TEXT join.
    items: List[Dict[str, Any]] = []
    try:
        async with player_pool.acquire() as conn:
            pre = await conn.fetch(
                """
                WITH s AS (
                  SELECT
                    card_id, platform, suggestion_type,
                    current_price, target_price, expected_profit,
                    risk_level, confidence_score, priority_score,
                    reasoning, time_to_profit, market_state, created_at
                  FROM smart_buy_suggestions
                  WHERE platform = $1
                    AND COALESCE(current_price, 0) <= $2
                  ORDER BY created_at DESC
                  LIMIT $3
                )
                SELECT
                  s.*,
                  fp.name, fp.rating, fp.version, fp.image_url,
                  fp.club, fp.league, fp.nation, fp.position, fp.altposition
                FROM s
                LEFT JOIN fut_players fp
                  ON fp.card_id = s.card_id
                """,
                plat, budget, limit,
            )
            items = [dict(r) for r in pre]
    except Exception:
        # If the table isn't there or is in a different DB, we just fallback below.
        items = []

    if items:
        return {"items": items, "count": len(items)}

    # 2) Fallback: synthesize suggestions from fut_players table (works even if smart_buy_* tables are empty)
    try:
        async with player_pool.acquire() as conn:
            # price is TEXT → cast carefully; ignore blanks / zero
            candidates = await conn.fetch(
                """
                SELECT
                  card_id,
                  name, rating, version, image_url,
                  club, league, nation, position, altposition,
                  COALESCE(NULLIF(price, '')::int, 0) AS price_int
                FROM fut_players
                WHERE COALESCE(NULLIF(price, '')::int, 0) BETWEEN 1500 AND $1
                ORDER BY rating DESC NULLS LAST, name ASC
                LIMIT $2
                """,
                budget,
                max(30, limit * 3),
            )
    except Exception as e:
        raise HTTPException(500, f"players source error: {e}")

    # Rank candidates by a rough priority score:
    # higher rating gets a bump; mid-range prices are often liquid; add a tiny random to break ties
    scored: List[tuple[float, asyncpg.Record]] = []
    for r in candidates:
        price = int(r["price_int"] or 0)
        if price <= 0:
            continue
        rating = int(r["rating"] or 0)
        mid_bonus = 1.0 - abs((price / max(1, budget)) - 0.5)  # peak near budget/2
        score = 0.6 * (rating / 100.0) + 0.3 * mid_bonus + 0.1 * random.random()
        scored.append((score, r))

    scored.sort(reverse=True)
    picked = [r for _, r in scored[:limit]]

    out: List[Dict[str, Any]] = []
    lo, hi = risk_pct
    for r in picked:
        price = int(r["price_int"])
        bump = random.uniform(lo, hi) + horizon_bonus
        target = int(round(price * (1.0 + bump)))
        # expected profit after 5% tax on sell
        expected_profit = int(round(target * 0.95 - price))
        expected_profit = max(expected_profit, 0)

        # Heuristic risk label mirrors input
        risk_level = risk_tolerance

        # Simple confidence from rating & price proximity to budget
        rating = int(r["rating"] or 0)
        proximity = 1.0 - abs((price / max(1, budget)) - 0.6)
        confidence = max(0, min(100, int(50 + 40 * proximity + 0.1 * rating)))

        # Priority mixes confidence, rating, and how affordable it is
        priority = max(0, min(100, int(0.5 * confidence + 0.4 * (rating) + 10 * (price < budget))))

        out.append({
            "card_id": r["card_id"],              # TEXT
            "platform": plat,
            "suggestion_type": "buy-dip" if bump < (hi + horizon_bonus) else "buy-flip",
            "current_price": price,
            "target_price": target,
            "expected_profit": expected_profit,
            "risk_level": risk_level,
            "confidence_score": confidence,
            "priority_score": priority,
            "reasoning": "Price fits budget with healthy upside vs. EA tax; liquidity heuristics favourable.",
            "time_to_profit": {"short": "6-24h", "medium": "1-3d", "long": "3-7d"}[time_horizon],
            "market_state": "normal",
            "created_at": _now_utc_iso(),
            # metadata
            "name": r["name"],
            "rating": r["rating"],
            "version": r["version"],
            "image_url": r["image_url"],
            "club": r["club"],
            "league": r["league"],
            "nation": r["nation"],
            "position": r["position"],
            "altposition": r["altposition"],
        })

    return {"items": out, "count": len(out)}

# ------------------------------
# Feedback endpoint
# ------------------------------

@router.post("/feedback")
async def post_feedback(
    payload: FeedbackIn,
    request: Request,
    user_id: str = Depends(_get_current_user),
) -> Dict[str, Any]:
    pool, _ = _get_pools(request)
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO smart_buy_feedback
                  (user_id, card_id, action, notes, actual_buy_price, actual_sell_price, timestamp)
                VALUES
                  ($1, $2, $3, $4, $5, $6, NOW())
                """,
                user_id,
                payload.card_id,  # TEXT
                payload.action,
                payload.notes,
                payload.actual_buy_price,
                payload.actual_sell_price,
            )
    except Exception as e:
        raise HTTPException(500, f"feedback save failed: {e}")

    return {"ok": True}

# ------------------------------
# Stats endpoint
# ------------------------------

@router.get("/stats")
async def smart_buy_stats(request: Request) -> Dict[str, Any]:
    pool, _ = _get_pools(request)
    totals = {"suggestions": 0, "feedback": 0, "market_states": 0}
    try:
        async with pool.acquire() as conn:
            r = await conn.fetchrow(
                """
                SELECT
                  (SELECT COUNT(*) FROM smart_buy_suggestions) AS s,
                  (SELECT COUNT(*) FROM smart_buy_feedback)    AS f,
                  (SELECT COUNT(*) FROM market_states)         AS m
                """
            )
            if r:
                totals["suggestions"] = int(r["s"] or 0)
                totals["feedback"] = int(r["f"] or 0)
                totals["market_states"] = int(r["m"] or 0)
    except Exception:
        # If any table is missing, keep zeros — endpoint should not 404
        pass

    return {"totals": totals, "at": _now_utc_iso()}
