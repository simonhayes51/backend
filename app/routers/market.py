from fastapi import APIRouter, Depends, HTTPException
from typing import List, Dict, Any
from app.db import get_db
from app.services.indicators import ema, rsi, bollinger, atr

router = APIRouter(prefix="/api/market", tags=["Market"])

@router.get("/candles")
async def get_candles(
    player_card_id: str,
    platform: str = "ps",
    timeframe: str = "15m",
    limit: int = 300,
    db=Depends(get_db),
) -> List[Dict[str, Any]]:
    rows = await db.fetch(
        """
        SELECT open_time, open, high, low, close, volume
        FROM fut_candles
        WHERE player_card_id=$1 AND platform=$2 AND timeframe=$3
        ORDER BY open_time DESC
        LIMIT $4
        """,
        player_card_id, platform, timeframe, limit,
    )
    return [dict(r) for r in rows][::-1]  # ascending order

@router.get("/indicators")
async def get_indicators(
    player_card_id: str,
    platform: str = "ps",
    timeframe: str = "15m",
    db=Depends(get_db),
) -> Dict[str, Any]:
    candles = await get_candles(player_card_id, platform, timeframe, 500, db)
    if not candles:
        raise HTTPException(404, "No candles")

    closes = [c["close"] for c in candles]
    highs  = [c["high"] for c in candles]
    lows   = [c["low"]  for c in candles]

    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    mid, up, lo = bollinger(closes, 20, 2)
    rsi14 = rsi(closes, 14)
    atr14 = atr(highs, lows, closes, 14)

    return {
        "ema20": ema20,
        "ema50": ema50,
        "bb_upper": up,
        "bb_lower": lo,
        "rsi14": rsi14,
        "atr14": atr14,
        "count": len(closes),
    }
