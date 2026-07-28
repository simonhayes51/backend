# app/routers/v2/players.py
#
# One call for the Player Page's above-the-fold panels, instead of the
# frontend making 4+ separate fetches. This is purely an aggregation
# layer over already-live handlers/services - no scoring/market logic is
# duplicated here. Sales/BIN/candle history are deliberately excluded:
# they're independently timeframe-toggled, so the v2 frontend calls
# those v1 endpoints directly (see the v2 plan, Phase 1, section 1.4).
from __future__ import annotations

import asyncio
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from app.auth.entitlements import compute_entitlements
from app.db import get_player_pool
from app.routers.fair_value import card_fair_value
from app.routers.players import (
    get_player,
    get_player_lazy_buyer_score_route,
    get_player_market_metrics_route,
)
from app.routers.v2.analytics import get_card_scores
from app.routers.v2.recommendations import get_player_recommendation
from app.services.deal_confidence import compute_deal_confidence

router = APIRouter(tags=["v2-players"])


async def _safe(coro) -> Any:
    """One failing/gated panel must not 500 the whole summary response -
    the frontend renders each section independently and can show its own
    error/locked state for just that panel."""
    try:
        return await coro
    except HTTPException as e:
        return {"error": e.detail, "status": e.status_code}
    except Exception as e:
        return {"error": str(e), "status": 500}


async def _market_metrics(card_id: int) -> Any:
    pool = await get_player_pool()
    async with pool.acquire() as conn:
        return await get_player_market_metrics_route(card_id, conn)


async def _lazy_buyer_score(card_id: int) -> Any:
    pool = await get_player_pool()
    async with pool.acquire() as conn:
        return await get_player_lazy_buyer_score_route(card_id, conn)


@router.get("/players/{card_id}/summary")
async def player_summary(card_id: int, request: Request) -> Dict[str, Any]:
    pool = await get_player_pool()
    async with pool.acquire() as conn:
        meta = await get_player(str(card_id), conn)  # 404s naturally, propagates below

    (
        market_metrics, fair_value, lazy_buyer_score, deal_confidence,
        card_scores, recommendation, ent,
    ) = await asyncio.gather(
        _safe(_market_metrics(card_id)),
        _safe(card_fair_value(card_id, request)),
        _safe(_lazy_buyer_score(card_id)),
        _safe(compute_deal_confidence(card_id)),
        _safe(get_card_scores(card_id, request)),
        _safe(get_player_recommendation(card_id, request)),
        compute_entitlements(request),
    )

    return {
        "card_id": card_id,
        "meta": meta,
        "market_metrics": market_metrics,
        "fair_value": fair_value,
        "lazy_buyer_score": lazy_buyer_score,
        "deal_confidence": deal_confidence,
        "card_scores": card_scores,
        "recommendation": recommendation,
        "entitlements": {"tier": ent["tier"], "features": ent["features"]},
    }
