# app/routers/v2/recommendations.py
#
# Read-only endpoints over recommendations/recommendations_latest
# (migration 021, extended by migration 024 with the V1.2 columns).
# require_feature("ai_recommendations") gates the single-card route;
# require_feature("opportunity_feed") gates the feed routes. Gated
# INLINE (not via a route Depends()) - the single-card route is called
# directly by app/routers/v2/players.py's player_summary(), which
# bypasses a route-decorator Depends() entirely (that's only enforced by
# FastAPI's own request pipeline, not a plain in-process function call).
#
# Strategy-specific feeds (STRATEGY_FEEDS below) replace a single mixed
# "opportunities" board with the separate Quick Flips/Low-Risk/Lazy
# Buyer/Long Hold/SBC views the V1.2 spec calls for - each ranked by its
# own fields, not one global score. `opportunities`/`high-confidence`/
# `avoid` are kept as they were (still work correctly against V1.2 rows,
# since recommendation_engine_v2.py derives the legacy recommendation/
# confidence columns from the correct after-tax numbers rather than
# writing a second, independently-computed opinion).
from __future__ import annotations

import json
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Query, Request

from app.auth.entitlements import require_feature

router = APIRouter(tags=["v2-recommendations"])

_JSONB_COLUMNS = (
    "market_drivers", "similar_events",
    "qualified_strategies", "strategy_results", "failed_gate_reasons", "held_decision_reasons",
)


def _player_pool(request: Request):
    pool = getattr(request.app.state, "player_pool", None)
    if pool is None:
        raise HTTPException(503, "player pool not ready")
    return pool


def _row_to_dict(row) -> Dict[str, Any]:
    d = dict(row)
    for key in _JSONB_COLUMNS:
        if isinstance(d.get(key), str):
            d[key] = json.loads(d[key])
    if d.get("computed_at"):
        d["computed_at"] = d["computed_at"].isoformat()
    # expected_net_roi is deliberately null until a validated ML model
    # exists (see recommendation_engine_v2.py) - expose the reason
    # alongside the null so API consumers don't have to guess why.
    if d.get("expected_net_roi") is None and "expected_net_roi_source" not in d:
        d["expected_net_roi_source"] = "unavailable_until_validated_model"
    return d


@router.get("/players/{card_id}/recommendation")
async def get_player_recommendation(card_id: int, request: Request) -> Dict[str, Any]:
    await require_feature("ai_recommendations")(request)
    pool = _player_pool(request)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM recommendations_latest WHERE card_id = $1 AND platform = 'ps'",
            card_id,
        )
    if not row:
        raise HTTPException(404, "No recommendation computed for this card yet")
    return _row_to_dict(row)


async def _feed(pool, where: str, order: str, limit: int) -> Dict[str, Any]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT r.*, p.name, p.rating, p.version, p.image_url,
                   p.card_bg_image, p.card_cutout_image, p.card_cutout_type, p.card_name
            FROM recommendations_latest r
            LEFT JOIN fut_players p ON p.card_id = r.card_id
            WHERE {where}
            ORDER BY {order}
            LIMIT $1
            """,
            limit,
        )
    items = [_row_to_dict(r) for r in rows]
    return {"items": items, "count": len(items)}


@router.get("/recommendations/opportunities")
async def opportunities(request: Request, limit: int = Query(20, ge=1, le=100)) -> Dict[str, Any]:
    await require_feature("opportunity_feed")(request)
    # r.status is the real V1.2 decision field - r.recommendation is only
    # kept as a derived, deprecated compatibility column (see migration
    # 024's docstring), so new queries read status directly.
    return await _feed(_player_pool(request), "r.status = 'BUY'", "r.confidence DESC NULLS LAST", limit)


@router.get("/recommendations/high-confidence")
async def high_confidence(request: Request, limit: int = Query(20, ge=1, le=100), min_confidence: float = Query(70, ge=0, le=100)) -> Dict[str, Any]:
    await require_feature("opportunity_feed")(request)
    pool = _player_pool(request)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT r.*, p.name, p.rating, p.version, p.image_url,
                   p.card_bg_image, p.card_cutout_image, p.card_cutout_type, p.card_name
            FROM recommendations_latest r
            LEFT JOIN fut_players p ON p.card_id = r.card_id
            WHERE r.status = 'BUY' AND r.confidence >= $1
            ORDER BY r.confidence DESC NULLS LAST
            LIMIT $2
            """,
            min_confidence, limit,
        )
    items = [_row_to_dict(r) for r in rows]
    return {"items": items, "count": len(items)}


@router.get("/recommendations/avoid")
async def avoid(request: Request, limit: int = Query(20, ge=1, le=100)) -> Dict[str, Any]:
    await require_feature("opportunity_feed")(request)
    # AVOID only - not a combined "avoid or sell" board. There is
    # currently no batch source of SELL signals: those require a real
    # held_purchase_price, which recommendation_engine_v2.py never looks
    # up automatically (no table stores it - see that module's
    # docstring), only run_pass_v2()'s fresh-buy sweep populates
    # recommendations_latest in bulk. A SELL feed would need a portfolio/
    # holdings table this schema doesn't have yet.
    return await _feed(_player_pool(request), "r.status = 'AVOID'", "r.computed_at DESC", limit)


# Each strategy's own ranking fields, per the V1.2 spec - never a single
# cross-strategy score. jsonb containment (@>) on qualified_strategies
# is the qualification filter; ORDER BY is strategy-specific.
_STRATEGY_ORDER: Dict[str, str] = {
    "quick_flip": "r.score_liquidity DESC NULLS LAST, r.likely_net_roi DESC NULLS LAST, r.score_confidence DESC NULLS LAST",
    "swing_trade": "r.likely_net_roi DESC NULLS LAST, r.score_confidence DESC NULLS LAST",
    "low_risk": "r.score_confidence DESC NULLS LAST, r.score_risk ASC NULLS LAST, r.conservative_net_roi DESC NULLS LAST",
    "long_hold": "r.bullish_net_roi DESC NULLS LAST, r.score_momentum DESC NULLS LAST, r.score_confidence DESC NULLS LAST",
    "lazy_buyer": "r.score_liquidity DESC NULLS LAST, r.likely_net_roi DESC NULLS LAST",
    "sbc": "r.likely_net_roi DESC NULLS LAST",
}


@router.get("/recommendations/highest-likely-roi")
async def highest_likely_roi(request: Request, limit: int = Query(20, ge=1, le=100)) -> Dict[str, Any]:
    """Ranked by likely_net_roi alone, among cards that qualified for at
    least one real strategy - not merely "positive discount" like the
    old expected_roi_pct-sorted board."""
    await require_feature("opportunity_feed")(request)
    return await _feed(
        _player_pool(request),
        "r.status = 'BUY' AND jsonb_array_length(r.qualified_strategies) > 0",
        "r.likely_net_roi DESC NULLS LAST",
        limit,
    )


@router.get("/recommendations/strategy/{strategy_name}")
async def strategy_feed(strategy_name: str, request: Request, limit: int = Query(20, ge=1, le=100)) -> Dict[str, Any]:
    if strategy_name not in _STRATEGY_ORDER:
        raise HTTPException(404, f"Unknown strategy: {strategy_name}")
    await require_feature("opportunity_feed")(request)
    order = _STRATEGY_ORDER[strategy_name]
    where = "r.qualified_strategies @> $2::jsonb"
    pool = _player_pool(request)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT r.*, p.name, p.rating, p.version, p.image_url,
                   p.card_bg_image, p.card_cutout_image, p.card_cutout_type, p.card_name
            FROM recommendations_latest r
            LEFT JOIN fut_players p ON p.card_id = r.card_id
            WHERE {where}
            ORDER BY {order}
            LIMIT $1
            """,
            limit, json.dumps([strategy_name]),
        )
    items = [_row_to_dict(r) for r in rows]
    return {"strategy": strategy_name, "items": items, "count": len(items)}
