# app/routers/fair_value.py
"""
Fair Value + Undervalued board + Anomaly radar — the consumer-facing routes
over fair_value_mv (see app/services/fair_value.py).

Gating (per the revised tier model):
  - Single-card fair value: FREE gets a teaser (direction + rough band),
    Pro+ gets exact numbers. The teaser is the conversion hook: show the
    value exists, gate the precision.
  - Undervalued board: Pro+.
  - Anomaly radar: Elite.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.auth.entitlements import compute_entitlements, require_feature
from app.services import fair_value as fv

router = APIRouter(prefix="/api/market", tags=["fair-value"])


def _player_pool(request: Request):
    pool = getattr(request.app.state, "player_pool", None)
    if pool is None:
        raise HTTPException(503, "player pool not ready")
    return pool


def _teaser(row: Dict[str, Any]) -> Dict[str, Any]:
    """Free-tier view: direction + rough band, exact numbers withheld."""
    discount = row.get("discount_pct")
    if discount is None:
        verdict = "unknown"
    elif discount >= 8:
        verdict = "steal"
    elif discount >= 3:
        verdict = "under"
    elif discount <= -5:
        verdict = "overpriced"
    else:
        verdict = "fair"
    return {
        "card_id": row["card_id"],
        "name": row["name"],
        "rating": row["rating"],
        "version": row["version"],
        "image_url": row["image_url"],
        "verdict": verdict,               # steal | under | fair | overpriced | unknown
        "sales_24h": row["sales_24h"],    # liquidity is free - it builds trust
        "locked": True,
        "upgrade_feature": "fair_value",
        "message": "Exact fair value, discount % and sell targets are a Pro thing. Level up to see the numbers.",
    }


@router.get("/fair-value/{card_id}")
async def card_fair_value(card_id: int, request: Request):
    pool = _player_pool(request)
    row = await fv.get_card_fair_value(pool, card_id)
    if not row:
        raise HTTPException(404, "No fair-value data for this card yet")

    ent = await compute_entitlements(request)
    if "fair_value" in ent["features"]:
        row["locked"] = False
        return row
    return _teaser(row)


@router.get("/undervalued", dependencies=[Depends(require_feature("undervalued_board"))])
async def undervalued_board(
    request: Request,
    limit: int = Query(30, ge=1, le=100),
    min_price: int = Query(1000, ge=0),
    max_price: Optional[int] = Query(None, ge=0),
    min_sales_24h: int = Query(5, ge=1),
    min_discount_pct: float = Query(3.0, ge=0),
):
    pool = _player_pool(request)
    items = await fv.get_undervalued(
        pool,
        limit=limit,
        min_price=min_price,
        max_price=max_price,
        min_sales_24h=min_sales_24h,
        min_discount_pct=min_discount_pct,
    )
    return {"items": items, "count": len(items)}


@router.get("/undervalued/teaser")
async def undervalued_teaser(request: Request):
    """Free-tier peek at the board: top 3 entries, names + verdicts only.
    Enough to prove the edge is real; not enough to trade off."""
    pool = _player_pool(request)
    items = await fv.get_undervalued(pool, limit=3)
    return {
        "items": [_teaser(r) for r in items],
        "locked": True,
        "total_hint": "There are more picks live on the board right now.",
    }


@router.get("/anomalies", dependencies=[Depends(require_feature("anomaly_alerts"))])
async def anomaly_radar(
    request: Request,
    limit: int = Query(30, ge=1, le=100),
    zscore: float = Query(-2.0, le=0),
    min_sales_24h: int = Query(8, ge=1),
):
    pool = _player_pool(request)
    items = await fv.get_anomalies(
        pool, limit=limit, zscore_threshold=zscore, min_sales_24h=min_sales_24h
    )
    return {"items": items, "count": len(items)}
