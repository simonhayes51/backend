
from __future__ import annotations
from typing import Dict, Any
from fastapi import APIRouter, HTTPException
from app.utils.timebox import now_utc, next_daily_london_hour

router = APIRouter()

@router.get("/api/events/next")
async def next_event(request) -> Dict[str, Any]:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        nxt = next_daily_london_hour(18)
        return {"name": "Daily Content Drop", "kind": "promo", "start_at": nxt.isoformat(), "confidence": "heuristic"}
    async with pool.acquire() as con:
        row = await con.fetchrow(
            "SELECT name, kind, start_at, confidence FROM events WHERE start_at > $1 ORDER BY start_at ASC LIMIT 1",
            now_utc()
        )
    if row:
        return {"name": row["name"], "kind": row["kind"], "start_at": row["start_at"].isoformat(), "confidence": row["confidence"]}
    nxt = next_daily_london_hour(18)
    return {"name": "Daily Content Drop", "kind": "promo", "start_at": nxt.isoformat(), "confidence": "heuristic"}
