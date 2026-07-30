# app/routers/v2/recommendations.py
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from app.auth.entitlements import require_feature
from app.services.player_card_ondemand import ensure_cards_requested

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


def _versioned_card_url(url: Optional[str], generated_at: Optional[datetime]) -> Optional[str]:
    if not url or not generated_at:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}v={int(generated_at.timestamp() * 1000)}"


def _row_to_dict(row) -> Dict[str, Any]:
    d = dict(row)
    for key in _JSONB_COLUMNS:
        if isinstance(d.get(key), str):
            d[key] = json.loads(d[key])
    if d.get("computed_at"):
        d["computed_at"] = d["computed_at"].isoformat()
    if d.get("expected_net_roi") is None and "expected_net_roi_source" not in d:
        d["expected_net_roi_source"] = "unavailable_until_validated_model"
    d["display_name"] = d.get("display_name") or d.get("nickname") or d.get("card_name") or d.get("name")
    d["generated_card_url"] = _versioned_card_url(
        d.get("generated_card_url"), d.get("generated_card_at")
    )
    if d.get("generated_card_at"):
        d["generated_card_at"] = d["generated_card_at"].isoformat()
    return d


_PLAYER_COLUMNS = """
    p.name, p.display_name, p.nickname, p.rating, p.version, p.position, p.image_url,
    p.card_bg_image, p.card_cutout_image, p.card_cutout_type, p.card_name,
    p.generated_card_url, p.generated_card_status, p.generated_card_at, p.generated_card_flagged,
    p.nation_image, p.league_image, p.club_image,
    p.pace, p.shooting, p.passing, p.dribbling, p.defending, p.physicality
"""


def _needs_card(d: Dict[str, Any]) -> bool:
    return d.get("generated_card_status") != "ready" or bool(d.get("generated_card_flagged"))


@router.get("/players/{card_id}/recommendation")
async def get_player_recommendation(card_id: int, request: Request) -> Dict[str, Any]:
    await require_feature("ai_recommendations")(request)
    async with _player_pool(request).acquire() as conn:
        row = await conn.fetchrow(
            f"""SELECT r.*, {_PLAYER_COLUMNS}
            FROM recommendations_latest r
            LEFT JOIN fut_players p ON p.card_id=r.card_id
            WHERE r.card_id=$1 AND r.platform='ps'""", card_id,
        )
    if not row:
        raise HTTPException(404, "No recommendation computed for this card yet")
    d = _row_to_dict(row)
    if _needs_card(d):
        await ensure_cards_requested(_player_pool(request), [str(card_id)])
    return d


@router.get("/recommendations/card-images")
async def generated_card_images(request: Request, card_ids: str = Query("")) -> Dict[str, Any]:
    ids = []
    for raw in card_ids.split(","):
        try:
            if raw.strip():
                ids.append(int(raw.strip()))
        except ValueError:
            pass
    ids = list(dict.fromkeys(ids))[:100]
    if not ids:
        return {"images": {}}
    pool = _player_pool(request)
    async with pool.acquire() as conn:
        all_rows = await conn.fetch(
            """SELECT card_id, generated_card_url, generated_card_at,
                      generated_card_status, generated_card_flagged
            FROM fut_players WHERE card_id=ANY($1::bigint[])""", ids,
        )

    needs_card = [
        str(r["card_id"]) for r in all_rows
        if r["generated_card_status"] != "ready" or r["generated_card_flagged"]
    ]
    if needs_card:
        await ensure_cards_requested(pool, needs_card)

    return {
        "images": {
            str(r["card_id"]): _versioned_card_url(
                r["generated_card_url"], r["generated_card_at"]
            )
            for r in all_rows
            if r["generated_card_status"] == "ready"
            and r["generated_card_url"]
            and not r["generated_card_flagged"]
        },
        "statuses": {
            str(r["card_id"]): r["generated_card_status"] for r in all_rows
        },
    }


async def _trigger_missing_cards(pool, items: list) -> None:
    needs_card = [str(item["card_id"]) for item in items if _needs_card(item)]
    if needs_card:
        await ensure_cards_requested(pool, needs_card)


async def _feed(pool, where: str, order: str, limit: int) -> Dict[str, Any]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"""SELECT r.*, {_PLAYER_COLUMNS}
            FROM recommendations_latest r LEFT JOIN fut_players p ON p.card_id=r.card_id
            WHERE {where} ORDER BY {order} LIMIT $1""", limit)
    items = [_row_to_dict(r) for r in rows]
    await _trigger_missing_cards(pool, items)
    return {"items": items, "count": len(items)}


@router.get("/recommendations/opportunities")
async def opportunities(request: Request, limit: int = Query(20, ge=1, le=100)) -> Dict[str, Any]:
    await require_feature("opportunity_feed")(request)
    return await _feed(_player_pool(request), "r.status='BUY'", "r.confidence DESC NULLS LAST", limit)


@router.get("/recommendations/high-confidence")
async def high_confidence(request: Request, limit: int=Query(20,ge=1,le=100), min_confidence: float=Query(70,ge=0,le=100)) -> Dict[str,Any]:
    await require_feature("opportunity_feed")(request)
    pool = _player_pool(request)
    async with pool.acquire() as conn:
        rows=await conn.fetch(f"""SELECT r.*, {_PLAYER_COLUMNS} FROM recommendations_latest r
        LEFT JOIN fut_players p ON p.card_id=r.card_id WHERE r.status='BUY' AND r.confidence >= $1
        ORDER BY r.confidence DESC NULLS LAST LIMIT $2""",min_confidence,limit)
    items=[_row_to_dict(r) for r in rows]
    await _trigger_missing_cards(pool, items)
    return {"items":items,"count":len(items)}


@router.get("/recommendations/avoid")
async def avoid(request: Request, limit: int=Query(20,ge=1,le=100))->Dict[str,Any]:
    await require_feature("opportunity_feed")(request)
    return await _feed(_player_pool(request),"r.status='AVOID'","r.computed_at DESC",limit)


_STRATEGY_ORDER: Dict[str,str] = {
 "quick_flip":"r.score_liquidity DESC NULLS LAST, r.likely_net_roi DESC NULLS LAST, r.score_confidence DESC NULLS LAST",
 "swing_trade":"r.likely_net_roi DESC NULLS LAST, r.score_momentum DESC NULLS LAST, r.score_confidence DESC NULLS LAST",
 "low_risk":"r.score_risk ASC NULLS LAST, r.score_confidence DESC NULLS LAST, r.conservative_net_roi DESC NULLS LAST",
 "long_hold":"r.score_momentum DESC NULLS LAST, r.bullish_net_roi DESC NULLS LAST, r.score_confidence DESC NULLS LAST",
 "lazy_buyer":"r.score_liquidity DESC NULLS LAST, r.likely_net_roi DESC NULLS LAST",
 "sbc":"COALESCE((r.strategy_results->'sbc'->>'score')::numeric,0) DESC, r.likely_net_roi DESC NULLS LAST",
}

_STRATEGY_FALLBACK = {
 "quick_flip":"r.status='BUY' AND r.score_liquidity >= 55 AND r.likely_net_roi > 0",
 "swing_trade":"r.status='BUY' AND r.score_momentum >= 45 AND r.likely_net_roi >= 3",
 "low_risk":"r.status='BUY' AND r.score_risk <= 45 AND r.score_confidence >= 55",
 "long_hold":"r.status='BUY' AND r.score_momentum >= 55 AND r.bullish_net_roi > 0",
 "lazy_buyer":"r.status='BUY' AND r.score_liquidity >= 45 AND r.likely_net_roi > 0",
 "sbc":"r.status='BUY' AND (r.market_drivers::text ILIKE '%sbc%' OR r.strategy_results ? 'sbc')",
}


@router.get("/recommendations/highest-likely-roi")
async def highest_likely_roi(request: Request, limit:int=Query(20,ge=1,le=100))->Dict[str,Any]:
    await require_feature("opportunity_feed")(request)
    return await _feed(_player_pool(request),"r.status='BUY' AND jsonb_array_length(r.qualified_strategies)>0","r.likely_net_roi DESC NULLS LAST",limit)


@router.get("/recommendations/strategy/{strategy_name}")
async def strategy_feed(strategy_name:str, request:Request, limit:int=Query(20,ge=1,le=100))->Dict[str,Any]:
    if strategy_name not in _STRATEGY_ORDER:
        raise HTTPException(404,f"Unknown strategy: {strategy_name}")
    await require_feature("opportunity_feed")(request)
    order=_STRATEGY_ORDER[strategy_name]
    pool=_player_pool(request)
    async with pool.acquire() as conn:
        rows=await conn.fetch(f"""SELECT r.*, {_PLAYER_COLUMNS} FROM recommendations_latest r
          LEFT JOIN fut_players p ON p.card_id=r.card_id
          WHERE r.qualified_strategies @> $2::jsonb ORDER BY {order} LIMIT $1""",limit,json.dumps([strategy_name]))
        if len(rows) < limit:
            existing=[r["card_id"] for r in rows]
            extra=await conn.fetch(f"""SELECT r.*, {_PLAYER_COLUMNS} FROM recommendations_latest r
              LEFT JOIN fut_players p ON p.card_id=r.card_id
              WHERE {_STRATEGY_FALLBACK[strategy_name]} AND NOT (r.card_id=ANY($2::bigint[]))
              ORDER BY {order} LIMIT $1""",limit-len(rows),existing or [-1])
            rows=list(rows)+list(extra)
    items=[_row_to_dict(r) for r in rows]
    await _trigger_missing_cards(pool, items)
    return {"strategy":strategy_name,"items":items,"count":len(items),"strict_count":len(items)-max(0,len(items)-limit)}
