# app/routers/v2/sbc.py
#
# Read-only endpoints over market_events/sbc_details/sbc_challenges/
# event_market_impact (migrations 018/019). Ungated for now - Phase 4
# wires require_feature("sbc_impact_predictions") onto the impact route.
from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Query, Request

from app.services import market_events as me

router = APIRouter(tags=["v2-sbc"])


def _player_pool(request: Request):
    pool = getattr(request.app.state, "player_pool", None)
    if pool is None:
        raise HTTPException(503, "player pool not ready")
    return pool


@router.get("/sbc/events")
async def list_sbc_events(
    request: Request,
    kind: str = "sbc",
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> Dict[str, Any]:
    pool = _player_pool(request)
    events = await me.get_events(pool, kind=kind, limit=limit, offset=offset)
    return {"items": events, "count": len(events)}


@router.get("/sbc/events/{event_id}")
async def get_sbc_event(event_id: int, request: Request) -> Dict[str, Any]:
    pool = _player_pool(request)
    event = await me.get_event(pool, event_id)
    if not event:
        raise HTTPException(404, "Event not found")
    return event


@router.get("/sbc/events/{event_id}/impact")
async def get_sbc_event_impact(event_id: int, request: Request) -> Dict[str, Any]:
    pool = _player_pool(request)
    event = await me.get_event(pool, event_id)
    if not event:
        raise HTTPException(404, "Event not found")
    impact = await me.get_event_impact(pool, event_id)
    return {"event_id": event_id, "items": impact, "count": len(impact)}
