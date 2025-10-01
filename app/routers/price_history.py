# app/routers/price_history.py
from typing import Literal
from fastapi import APIRouter, Query, Request
from app.services.price_history import get_price_history

router = APIRouter()  # mounted under "/api"

Timeframe = Literal["today", "3d", "week", "month", "year"]

@router.get("/price-history")
async def price_history(
    req: Request,
    playerId: int = Query(..., alias="playerId", description="FUT card ID"),
    platform: str = Query("ps", description="ps | xbox | pc"),
    tf: Timeframe = Query("today", description="today | 3d | week | month | year"),
):
    """
    Returns:
      { "points": [ { "t": ISO_8601_UTC, "price": int }, ... ] }
    Always 200; on errors returns empty series.
    """
    if playerId <= 0:
        return {"points": []}

    try:
        data = await get_price_history(card_id=playerId, platform=platform, tf=tf)
        return data
    except Exception as e:
        # Don't break the chart
        req.app.logger.warning(f"/price-history failed for {playerId}: {e}")
        return {"points": []}
