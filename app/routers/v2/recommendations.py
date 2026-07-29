# app/routers/v2/recommendations.py
from __future__ import annotations

import json
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Query, Request

from app.auth.entitlements import require_feature

router = APIRouter(tags=["v2-recommendations"])

_JSONB_COLUMNS = (
    "market_drivers", "similar_events", "qualified_strategies",
    "strategy_results", "failed_gate_reasons", "held_decision_reasons",
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
    if d.get("expected_net_roi") is None and "expected_net_roi_source" not in d:
        d["expected_net_roi_source"] = "unavailable_until_validated_model"
    return d


_PLAYER_COLUMNS = """
    p.name, p.rating, p.version, p.position, p.image_url,
    p.card_bg_image, p.card_cutout_image, p.card_cutout_type, p.card_name,
    p.generated_card_url, p.generated_card_status,
    p.nation_image, p.league_image, p.club_image,
    p.pace, p.shooting, p.passing, p.dribbling, p.defending, p.physicality
"""


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


@router.get("/recommendations/card-images")
async def generated_card_images(
    request: Request,
    card_ids: str = Query("", description="Comma-separated card IDs"),
) -> Dict[str, Any]:
    """Return the latest saved transparent card PNGs for a dashboard batch.

    This stays ungated because it contains artwork only, not recommendation
    logic. It lets compact dashboard lists use the already-generated image
    instead of rebuilding card layers in the browser or making one request
    per player.
    """
    ids = []
    for raw in card_ids.split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            ids.append(int(raw))
        except ValueError:
            continue
    ids = list(dict.fromkeys(ids))[:100]
    if not ids:
        return {"images": {}}

    async with _player_pool(request).acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT card_id, generated_card_url
            FROM fut_players
            WHERE card_id = ANY($1::bigint[])
              AND generated_card_status = 'ready'
              AND generated_card_url IS NOT NULL
            """,
            ids,
        )
    return {"images": {str(r["card_id"]): r["generated_card_url"] for r in rows}}


async def _feed(pool, where: str, order: str, limit: int) -> Dict[str, Any]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT r.*, {_PLAYER_COLUMNS}
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
    return await _feed(_player_pool(request), "r.status = 'BUY'", "r.confidence DESC NULLS LAST", limit)


@router.get("/recommendations/high-confidence")
async def high_confidence(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    min_confidence: float = Query(70, ge=0, le=100),
) -> Dict[str, Any]:
    await require_feature("opportunity_feed")(request)
    pool = _player_pool(request)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT r.*, {_PLAYER_COLUMNS}
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
    return await _feed(_player_pool(request), "r.status = 'AVOID'", "r.computed_at DESC", limit)


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
    await require_feature("opportunity_feed")(request)
    return await _feed(
        _player_pool(request),
        "r.status = 'BUY' AND jsonb_array_length(r.qualified_strategies) > 0",
        "r.likely_net_roi DESC NULLS LAST",
        limit,
    )


@router.get("/recommendations/strategy/{strategy_name}")
async def strategy_feed(
    strategy_name: str,
    request: Request,
    limit: int = Query(20, ge=1, le=100),
) -> Dict[str, Any]:
    if strategy_name not in _STRATEGY_ORDER:
        raise HTTPException(404, f"Unknown strategy: {strategy_name}")
    await require_feature("opportunity_feed")(request)
    order = _STRATEGY_ORDER[strategy_name]
    pool = _player_pool(request)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT r.*, {_PLAYER_COLUMNS}
            FROM recommendations_latest r
            LEFT JOIN fut_players p ON p.card_id = r.card_id
            WHERE r.qualified_strategies @> $2::jsonb
            ORDER BY {order}
            LIMIT $1
            """,
            limit, json.dumps([strategy_name]),
        )
    items = [_row_to_dict(r) for r in rows]
    return {"strategy": strategy_name, "items": items, "count": len(items)}
