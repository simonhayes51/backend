from typing import Literal
from fastapi import APIRouter, Query, Request
from app.services.price_history import get_price_history

router = APIRouter()  # mounted under "/api" in main.py

Timeframe = Literal["today", "3d", "week", "month", "year"]

@router.get("/price-history")
async def price_history(
    req: Request,
    playerId: int = Query(..., alias="playerId", description="FUT card ID"),
    platform: str = Query("ps", description="ps | xbox | pc"),
    tf: Timeframe = Query("today", description="today | 3d | week | month | year"),
):
    """
    Always 200. On errors returns {"points": []} so the chart doesn't crash.
    """
    if playerId <= 0:
        return {"points": []}
    try:
        return await get_price_history(card_id=playerId, platform=platform, tf=tf)
    except Exception as e:
        # log and fall back to empty data instead of 502
        try:
            req.app.logger.warning(f"/price-history failed for {playerId}: {e}")
        except Exception:
            pass
        return {"points": []}
