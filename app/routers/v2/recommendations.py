# app/routers/v2/recommendations.py
#
# Read-only endpoints over recommendations/recommendations_latest
# (migration 021). require_feature("ai_recommendations") gates the
# single-card route; require_feature("opportunity_feed") gates the
# three feed routes. Gated INLINE (not via a route Depends()) - the
# single-card route is called directly by
# app/routers/v2/players.py's player_summary(), which bypasses a
# route-decorator Depends() entirely (that's only enforced by FastAPI's
# own request pipeline, not a plain in-process function call).
from __future__ import annotations

import json
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Query, Request

from app.auth.entitlements import require_feature

router = APIRouter(tags=["v2-recommendations"])


def _player_pool(request: Request):
    pool = getattr(request.app.state, "player_pool", None)
    if pool is None:
        raise HTTPException(503, "player pool not ready")
    return pool


def _row_to_dict(row) -> Dict[str, Any]:
    d = dict(row)
    for key in ("market_drivers", "similar_events"):
        if isinstance(d.get(key), str):
            d[key] = json.loads(d[key])
    if d.get("computed_at"):
        d["computed_at"] = d["computed_at"].isoformat()
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
            SELECT r.*, p.name, p.rating, p.version, p.image_url
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
    return await _feed(_player_pool(request), "r.recommendation = 'buy'", "r.confidence DESC", limit)


@router.get("/recommendations/high-confidence")
async def high_confidence(request: Request, limit: int = Query(20, ge=1, le=100), min_confidence: float = Query(70, ge=0, le=100)) -> Dict[str, Any]:
    await require_feature("opportunity_feed")(request)
    pool = _player_pool(request)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT r.*, p.name, p.rating, p.version, p.image_url
            FROM recommendations_latest r
            LEFT JOIN fut_players p ON p.card_id = r.card_id
            WHERE r.recommendation = 'buy' AND r.confidence >= $1
            ORDER BY r.confidence DESC
            LIMIT $2
            """,
            min_confidence, limit,
        )
    items = [_row_to_dict(r) for r in rows]
    return {"items": items, "count": len(items)}


@router.get("/recommendations/avoid")
async def avoid(request: Request, limit: int = Query(20, ge=1, le=100)) -> Dict[str, Any]:
    await require_feature("opportunity_feed")(request)
    return await _feed(_player_pool(request), "r.recommendation = 'avoid'", "r.computed_at DESC", limit)
