# app/routers/v2/analytics.py
#
# Read-only endpoints over card_scores/card_scores_latest (migration
# 020). GET /scores stays free (mirrors fair_value's teaser/full split -
# free users see the current number, not the trend); GET /scores/history
# is the literal investment_score_history feature, gated inline (not via
# a route Depends()) so the same check applies whether the route is hit
# directly or this function is called in-process elsewhere later.
from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Query, Request

from app.auth.entitlements import require_feature

router = APIRouter(tags=["v2-analytics"])

MAX_BATCH_IDS = 100


def _player_pool(request: Request):
    pool = getattr(request.app.state, "player_pool", None)
    if pool is None:
        raise HTTPException(503, "player pool not ready")
    return pool


@router.get("/cards/{card_id}/scores")
async def get_card_scores(card_id: int, request: Request) -> Dict[str, Any]:
    pool = _player_pool(request)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT score_type, value, engine_version, computed_at FROM card_scores_latest WHERE card_id = $1 AND platform = 'ps'",
            card_id,
        )
    if not rows:
        raise HTTPException(404, "No scores computed for this card yet")
    return {"card_id": card_id, "scores": {r["score_type"]: float(r["value"]) for r in rows}, "computed_at": max(r["computed_at"] for r in rows).isoformat()}


@router.get("/cards/scores/batch")
async def get_card_scores_batch(ids: str, request: Request) -> Dict[str, Any]:
    try:
        card_ids = [int(x) for x in ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(400, "ids must be a comma-separated list of integers")
    card_ids = card_ids[:MAX_BATCH_IDS]

    pool = _player_pool(request)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT card_id, score_type, value FROM card_scores_latest WHERE card_id = ANY($1::bigint[]) AND platform = 'ps'",
            card_ids,
        )
    by_card: Dict[int, Dict[str, float]] = {}
    for r in rows:
        by_card.setdefault(r["card_id"], {})[r["score_type"]] = float(r["value"])
    items = [{"card_id": cid, "scores": scores} for cid, scores in by_card.items()]
    return {"items": items, "count": len(items)}


@router.get("/cards/{card_id}/scores/history")
async def get_card_score_history(
    card_id: int, request: Request,
    score_type: str = Query(..., description="e.g. investment, risk, opportunity"),
    days: int = Query(30, ge=1, le=365),
) -> Dict[str, Any]:
    await require_feature("investment_score_history")(request)
    pool = _player_pool(request)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT value, computed_at FROM card_scores
            WHERE card_id = $1 AND platform = 'ps' AND score_type = $2
              AND computed_at >= now() - ($3 || ' days')::interval
            ORDER BY computed_at ASC
            """,
            card_id, score_type, str(days),
        )
    return {
        "card_id": card_id, "score_type": score_type,
        "points": [{"value": float(r["value"]), "computed_at": r["computed_at"].isoformat()} for r in rows],
    }
