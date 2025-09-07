# app/routers/smart_buy.py
from __future__ import annotations

import math
import random
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
import asyncpg

router = APIRouter(prefix="/smart-buy", tags=["smart-buy"])

# -----------------------------
# Helpers: pools & normalization
# -----------------------------

async def _get_player_conn(request: Request) -> asyncpg.Connection:
    # Uses the same pool the rest of your app exposes in main.py
    if not getattr(request.app.state, "player_pool", None):
        # Fallback to primary pool if you didn't separate DBs
        if not getattr(request.app.state, "pool", None):
            raise HTTPException(500, "Database pool not available")
        return await request.app.state.pool.acquire()
    return await request.app.state.player_pool.acquire()

async def _release_player_conn(request: Request, conn: asyncpg.Connection):
    try:
        if getattr(request.app.state, "player_pool", None):
            await request.app.state.player_pool.release(conn)
        else:
            await request.app.state.pool.release(conn)
    except Exception:
        pass

def _norm_platform(p: str) -> str:
    p = (p or "").strip().lower()
    if p in ("ps", "playstation", "console"): return "ps"
    if p in ("xb", "xbox"): return "xbox"
    if p in ("pc", "origin"): return "pc"
    return "ps"

def _norm_risk(r: str) -> str:
    """Map all UI labels to three buckets."""
    r = (r or "").strip().lower()
    if r in ("low", "conservative", "safe"): return "conservative"
    if r in ("high", "aggressive", "risky"): return "aggressive"
    # treat anything else as "moderate"
    return "moderate"

def _norm_horizon(h: str) -> str:
    """Map all UI labels to three buckets."""
    h = (h or "").strip().lower()
    if "quick" in h or "short" in h or "1-6" in h: return "short"
    if "long" in h or "2-" in h or "7d" in h: return "long"
    return "medium"

def _profit_range_by_risk(risk: str) -> tuple[float, float]:
    """Return (min_pct, max_pct) expectation before EA tax; heuristic."""
    if risk == "conservative":
        return (2.0, 5.0)
    if risk == "aggressive":
        return (8.0, 15.0)
    return (4.0, 9.0)  # moderate

def _time_label(horizon: str) -> str:
    if horizon == "short": return "1-6h"
    if horizon == "medium": return "6-48h"
    return "2-7d"

def _ea_tax(sell_price: int) -> int:
    return int(round(sell_price * 0.05))

# -----------------------------
# Lightweight market intelligence
# -----------------------------

@router.get("/market-intelligence")
async def market_intelligence(request: Request, platform: str = Query("ps")):
    """
    Very lightweight 'state' signal derived from available fut_players prices.
    Not a trading signal — just a health/check summary for the UI.
    """
    platform = _norm_platform(platform)
    conn = await _get_player_conn(request)
    try:
        # Count how many rows have numeric prices and a nonzero value.
        row = await conn.fetchrow(
            """
            SELECT
              COUNT(*) FILTER (WHERE price ~ '^[0-9]+$') AS numeric_prices,
              COUNT(*) FILTER (WHERE price ~ '^[0-9]+$' AND price::int > 0) AS nonzero_prices,
              COUNT(*) AS total_rows
            FROM fut_players
            """
        )
        numeric = int(row["numeric_prices"] or 0)
        nonzero = int(row["nonzero_prices"] or 0)
        total = int(row["total_rows"] or 0)

        coverage = 0 if total == 0 else round(100.0 * nonzero / total, 1)
        # A simple confidence proxy: more valid prices -> higher confidence
        confidence = max(0, min(100, int(coverage)))

        state = "normal"
        if confidence < 25:
            state = "uncertain"
        elif confidence > 80:
            state = "normal"

        return {
            "platform": platform,
            "state": state,
            "confidence": confidence,
            "indicators": {
                "rows_total": total,
                "rows_priced_numeric": numeric,
                "rows_priced_nonzero": nonzero,
                "coverage_pct": coverage,
            },
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
    finally:
        await _release_player_conn(request, conn)

# -----------------------------
# Suggestions endpoint
# -----------------------------

@router.get("/suggestions")
async def smart_buy_suggestions(
    request: Request,
    budget: int = Query(100_000, ge=1, le=50_000_000),
    risk_tolerance: str = Query("moderate"),
    time_horizon: str = Query("short"),
    platform: str = Query("ps"),
    limit: int = Query(20, ge=1, le=100),
):
    """
    Produces buy suggestions from fut_players, using its price (text) cast to int.
    Ensures non-empty results by:
      - normalising risk/time labels from the UI,
      - sampling top rated affordable cards,
      - computing a target price with EA tax consideration.
    """
    plat = _norm_platform(platform)
    risk = _norm_risk(risk_tolerance)
    horizon = _norm_horizon(time_horizon)

    conn = await _get_player_conn(request)
    try:
        # Pull a generous window so we can score & downselect to 'limit'
        sample_cap = max(limit * 4, 60)

        rows = await conn.fetch(
            """
            WITH params AS (SELECT $1::int AS budget)
            SELECT
              card_id,
              name,
              rating,
              version,
              image_url,
              club,
              league,
              nation,
              position,
              altposition,
              price::int AS px
            FROM fut_players, params p
            WHERE price ~ '^[0-9]+$'
              AND price::int BETWEEN 1 AND p.budget
            ORDER BY rating DESC NULLS LAST, price::int ASC
            LIMIT $2
            """,
            budget,
            sample_cap,
        )

        if not rows:
            return {"items": [], "count": 0}

        min_pct, max_pct = _profit_range_by_risk(risk)
        time_label = _time_label(horizon)

        items: List[Dict[str, Any]] = []
        for r in rows:
            px = int(r["px"] or 0)
            if px <= 0:
                continue

            # Heuristic profit target based on risk band
            target_pct = random.uniform(min_pct, max_pct)
            target_raw = int(round(px * (1.0 + target_pct / 100.0)))
            tax = _ea_tax(target_raw)
            expected_profit_after_tax = (target_raw - px) - tax

            # Basic scoring: rating, profit size, cheaper cards slightly higher priority
            rating = int(r["rating"] or 0)
            profit_score = max(0, expected_profit_after_tax) / max(1, px)  # relative profit
            priority = int(round(50 + 0.35 * rating + 30 * profit_score))
            conf = int(round(55 + 0.25 * rating + 15 * (min(12_000, px) / 12_000.0)))  # tame to 0..100

            items.append({
                "card_id": str(r["card_id"]),
                "platform": plat,
                "suggestion_type": "buy-dip" if target_pct <= 6 else "buy-flip",
                "current_price": px,
                "target_price": target_raw,
                "expected_profit": expected_profit_after_tax,
                "risk_level": risk,
                "confidence_score": max(1, min(100, conf)),
                "priority_score": max(1, min(100, priority)),
                "reasoning": "Affordable vs budget; projected move within timeframe; EA tax considered",
                "time_to_profit": time_label,
                "market_state": "normal",
                "created_at": datetime.now(timezone.utc).isoformat(),

                # enrichment
                "name": r["name"],
                "rating": rating,
                "version": r["version"],
                "image_url": r["image_url"],
                "club": r["club"],
                "league": r["league"],
                "nation": r["nation"],
                "position": r["position"],
                "altposition": r["altposition"],
            })

            if len(items) >= limit:
                break

        return {"items": items, "count": len(items)}

    finally:
        await _release_player_conn(request, conn)

# -----------------------------
# Small stats block for the UI
# -----------------------------

@router.get("/stats")
async def smart_buy_stats(request: Request):
    """Tiny stats endpoint the UI can show under the module."""
    conn = await _get_player_conn(request)
    try:
        row = await conn.fetchrow(
            """
            SELECT
              COUNT(*) AS total,
              COUNT(*) FILTER (WHERE price ~ '^[0-9]+$') AS numeric_prices,
              COUNT(*) FILTER (WHERE price ~ '^[0-9]+$' AND price::int BETWEEN 1 AND 100000) AS under_100k
            FROM fut_players
            """
        )
        total = int(row["total"] or 0)
        numeric = int(row["numeric_prices"] or 0)
        under100k = int(row["under_100k"] or 0)
        return {
            "total_players": total,
            "priced_numeric": numeric,
            "priced_under_100k": under100k,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
    finally:
        await _release_player_conn(request, conn)
