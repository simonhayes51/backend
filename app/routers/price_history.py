# app/routers/price_history.py
from fastapi import APIRouter, Query, HTTPException
from typing import Literal
from app.services.price_history import get_price_history

router = APIRouter()  # no prefix here; we add "/api" when mounting in main.py

# Allowed timeframes to mirror the frontend pills
Timeframe = Literal["today", "3d", "week", "month", "year"]

@router.get("/price-history")
async def price_history(
    playerId: int = Query(..., alias="playerId", description="FUT card ID (same one your UI uses)"),
    platform: str = Query("ps", description="Platform hint (kept for API parity)"),
    tf: Timeframe = Query("today", description="Timeframe: today | 3d | week | month | year"),
):
    """
    Returns price history points for the chart.

    Response shape:
      {
        "points": [
          { "t": ISO_8601_UTC, "price": int },
          ...
        ]
      }
    """
    if playerId <= 0:
        raise HTTPException(status_code=400, detail="playerId must be a positive integer")

    try:
        data = await get_price_history(playerId, platform, tf)
    except Exception as e:
        # Surface a clean 502 if upstream (FUTBIN) fails
        raise HTTPException(status_code=502, detail=f"Upstream error: {e}") from e

    return data
