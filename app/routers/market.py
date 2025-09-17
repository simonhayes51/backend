# app/routers/market.py 
from fastapi import APIRouter, Depends, HTTPException
from typing import List, Dict, Any
from app.db import get_db
from app.services.indicators import ema, rsi, bollinger, atr

router = APIRouter(prefix="/api/market", tags=["Market"])

# REPLACE the /now endpoint in app/routers/market.py with this:

@router.get("/now")
async def get_current_price(
    player_card_id: str,
    platform: str = "ps",
    db=Depends(get_db),
) -> Dict[str, Any]:
    """Get current market price for a player"""
    try:
        from main import app
        
        # Get from fut_players table
        async with app.state.player_pool.acquire() as pconn:
            player_row = await pconn.fetchrow(
                "SELECT price_num, price, name FROM fut_players WHERE card_id=$1",
                player_card_id
            )
            
            if player_row:
                price = None
                if player_row["price_num"]:
                    price = int(player_row["price_num"])
                elif player_row["price"] and str(player_row["price"]).isdigit():
                    price = int(player_row["price"])
                
                if price:
                    from datetime import datetime
                    return {
                        "player_card_id": player_card_id,
                        "platform": platform,
                        "price": price,
                        "player_name": player_row["name"],
                        "timestamp": datetime.now().isoformat()
                    }
        
        # Fallback: try candle data
        candle_row = await db.fetchrow(
            """
            SELECT close as price, open_time
            FROM fut_candles
            WHERE player_card_id=$1 AND platform=$2 
            ORDER BY open_time DESC
            LIMIT 1
            """,
            player_card_id, platform
        )
        
        if candle_row:
            return {
                "player_card_id": player_card_id,
                "platform": platform,
                "price": int(candle_row["price"]),
                "timestamp": candle_row["open_time"].isoformat() if candle_row["open_time"] else None
            }
        
        raise HTTPException(404, f"No price data found for player {player_card_id}")
        
    except HTTPException:
        raise
    except Exception as e:
        import logging
        logging.error(f"Current price error: {e}")
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
