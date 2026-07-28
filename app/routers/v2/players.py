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

from app.auth.entitlements import compute_entitlements, require_feature
from app.db import get_player_pool
from app.futbin_client import fetch_card_layers
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


async def _with_live_card_layers(meta: Dict[str, Any]) -> Dict[str, Any]:
    """card_bg_image/card_cutout_image are only ever populated by
    auto_sync's futbin_card_art_backfill.py, which - unlike the collectors
    that came before it - has never actually been scheduled as a Railway
    Cron Job (see that file's own README section: it was deliberately left
    unscheduled pending a live-network verification pass). So in practice
    that column is null for nearly every card today. GET
    /api/fut-player-definition/{card_id} already solves exactly this for
    v1's Player Search page by fetching the same layers live, per request,
    off meta's player_url - viable here because this is a single card, not
    a list surface (fetching per-row for N list items is what the batch
    backfill worker exists to avoid)."""
    if meta.get("card_bg_image") or not meta.get("player_url"):
        return meta
    layers = await fetch_card_layers(meta["player_url"])
    if layers.get("bgImageUrl"):
        meta = dict(meta)
        meta["card_bg_image"] = layers["bgImageUrl"]
        meta["card_cutout_image"] = layers.get("cutoutImageUrl")
        meta["card_cutout_type"] = layers.get("cutoutType")
        meta["card_name"] = layers.get("cardName") or meta.get("card_name")
    return meta


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


async def _deal_confidence(card_id: int, request: Request) -> Any:
    # compute_deal_confidence() is a bare service function with no gate
    # of its own - main.py's /api/deal-confidence/{card_id} route gates
    # itself, but that route isn't what's called here, so the same
    # check is applied inline to keep the two consistent.
    await require_feature("deal_confidence")(request)
    return await compute_deal_confidence(card_id)


@router.get("/players/{card_id}/summary")
async def player_summary(card_id: int, request: Request) -> Dict[str, Any]:
    pool = await get_player_pool()
    async with pool.acquire() as conn:
        meta = await get_player(str(card_id), conn)  # 404s naturally, propagates below
    meta = await _with_live_card_layers(meta)

    (
        market_metrics, fair_value, lazy_buyer_score, deal_confidence,
        card_scores, recommendation, ent,
    ) = await asyncio.gather(
        _safe(_market_metrics(card_id)),
        _safe(card_fair_value(card_id, request)),
        _safe(_lazy_buyer_score(card_id)),
        _safe(_deal_confidence(card_id, request)),
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
