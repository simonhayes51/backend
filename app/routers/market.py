# app/routers/market.py 
from fastapi import APIRouter, Depends, HTTPException
from typing import List, Dict, Any
from app.db import get_db
from app.services.indicators import ema, rsi, bollinger, atr

router = APIRouter(prefix="/api/market", tags=["Market"])

# Add this to app/routers/market.py

@router.get("/now")
async def get_current_price(
    player_card_id: str,
    platform: str = "ps",
    db=Depends(get_db),
) -> Dict[str, Any]:
    """Get current market price for a player"""
    try:
        # Try to get the most recent candle data
        row = await db.fetchrow(
            """
            SELECT close as price, open_time
            FROM public.fut_candles
            WHERE player_card_id=$1 AND platform=$2 
            ORDER BY open_time DESC
            LIMIT 1
            """,
            player_card_id, platform
        )
        
        if row:
            return {
                "player_card_id": player_card_id,
                "platform": platform,
                "price": float(row["price"]),
                "timestamp": row["open_time"].isoformat() if row["open_time"] else None
            }
        else:
            # Fallback to fut_players table
            async with db.acquire() as player_conn:
                player_row = await player_conn.fetchrow(
                    "SELECT price_num FROM fut_players WHERE card_id=$1::text",
                    player_card_id
                )
                if player_row and player_row["price_num"]:
                    return {
                        "player_card_id": player_card_id,
                        "platform": platform,
                        "price": float(player_row["price_num"]),
                        "timestamp": None
                    }
            
            raise HTTPException(404, f"No price data found for player {player_card_id}")
            
    except Exception as e:
        raise HTTPException(500, f"Failed to get current price: {e}")

@router.get("/candles")
async def get_candles(
    player_card_id: str,
    platform: str = "ps",
    timeframe: str = "15m",
    limit: int = 300,
    db=Depends(get_db),
) -> List[Dict[str, Any]]:
    try:
        rows = await db.fetch(
            """
            SELECT open_time, open, high, low, close, volume
            FROM public.fut_candles
            WHERE player_card_id=$1 AND platform=$2 AND timeframe=$3
            ORDER BY open_time DESC
            LIMIT $4
            """,
            player_card_id, platform, timeframe, limit  # ← REMOVE the comma here
        )
        return [dict(r) for r in rows][::-1]
    except Exception as e:
        raise HTTPException(500, f"candles query failed: {e}")

@router.get("/indicators")
async def get_indicators(
    player_card_id: str,
    platform: str = "ps",
    timeframe: str = "15m",
    db=Depends(get_db),
) -> Dict[str, Any]:
    rows = await db.fetch(
        """
        SELECT open_time, open, high, low, close, volume
        FROM public.fut_candles
        WHERE player_card_id=$1 AND platform=$2 AND timeframe=$3
        ORDER BY open_time ASC
        """,
        player_card_id, platform, timeframe,
    )
    if not rows:
        raise HTTPException(404, "No candles for that player/platform/timeframe")

    closes = [r["close"] for r in rows]
    highs  = [r["high"]  for r in rows]
    lows   = [r["low"]   for r in rows]

    out: Dict[str, Any] = {}
    out["ema20"] = ema(closes, 20)
    out["ema50"] = ema(closes, 50)
    _, up, lo = bollinger(closes, 20, 2)
    out["bb_upper"] = up
    out["bb_lower"] = lo
    out["rsi14"] = rsi(closes, 14)
    out["atr14"] = atr(highs, lows, closes, 14)
    out["count"] = len(closes)
    return out
