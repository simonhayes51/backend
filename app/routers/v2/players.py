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

# Bounded, not fetch_card_layers's own 15s REQUEST_TIMEOUT: that constant
# is shared with fetch_price_by_url/fetch_recent_sales and is fine for
# those (nothing else is waiting on them), but this call sits directly in
# the critical path of every Player Page load - a slow or blocked
# futbin.com response must not hang the whole endpoint. 3s is enough for
# a normal page fetch and still fails fast if futbin.com is unreachable.
_LIVE_CARD_LAYERS_TIMEOUT = 1.5
_META_TIMEOUT = 2.5
_CRITICAL_PANEL_TIMEOUT = 2.5
_OPTIONAL_PANEL_TIMEOUT = 0.75


async def _live_card_layers(meta: Dict[str, Any]) -> Any:
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
    backfill worker exists to avoid).

    Two real bugs in the first version of this: it awaited before the
    summary endpoint's asyncio.gather() rather than as part of it (so its
    full latency - up to fetch_card_layers's own 15s timeout - serially
    added to every page load instead of overlapping with the other,
    much-faster panels), and it had no exception handling at all, so a
    parse_card_layers() failure on any real, unexpected futbin.com HTML
    (parse_card_layers is called *outside* fetch_card_layers's own
    try/except) 500'd the entire summary endpoint rather than just
    skipping the card art enhancement."""
    if meta.get("generated_card_url") or meta.get("card_bg_image") or not meta.get("player_url"):
        return None
    try:
        return await asyncio.wait_for(fetch_card_layers(meta["player_url"]), timeout=_LIVE_CARD_LAYERS_TIMEOUT)
    except Exception:
        return None


async def _safe(coro, *, timeout: float = _CRITICAL_PANEL_TIMEOUT) -> Any:
    """One failing/gated panel must not 500 the whole summary response -
    the frontend renders each section independently and can show its own
    error/locked state for just that panel."""
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        return {"error": "panel timed out", "status": 504}
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
        # Meta is the only hard dependency for rendering the page. Keep it
        # bounded too: a saturated database must produce a fast error rather
        # than leave the browser waiting for minutes.
        meta = await asyncio.wait_for(
            get_player(str(card_id), conn),
            timeout=_META_TIMEOUT,
        )  # 404s naturally, propagates below

    (
        market_metrics, fair_value, lazy_buyer_score, deal_confidence,
        card_scores, recommendation, ent, live_layers,
    ) = await asyncio.gather(
        _safe(_market_metrics(card_id)),
        _safe(card_fair_value(card_id, request)),
        # These panels are supplementary and the Lazy Buyer calculation can
        # rank the entire tracked market on a cold worker. Never let either
        # delay the core player answer.
        _safe(_lazy_buyer_score(card_id), timeout=_OPTIONAL_PANEL_TIMEOUT),
        _safe(_deal_confidence(card_id, request), timeout=_OPTIONAL_PANEL_TIMEOUT),
        _safe(get_card_scores(card_id, request), timeout=_OPTIONAL_PANEL_TIMEOUT),
        _safe(get_player_recommendation(card_id, request)),
        compute_entitlements(request),
        _live_card_layers(meta),
    )
    if live_layers and live_layers.get("bgImageUrl"):
        meta = dict(meta)
        meta["card_bg_image"] = live_layers["bgImageUrl"]
        meta["card_cutout_image"] = live_layers.get("cutoutImageUrl")
        meta["card_cutout_type"] = live_layers.get("cutoutType")
        meta["card_name"] = live_layers.get("cardName") or meta.get("card_name")

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
