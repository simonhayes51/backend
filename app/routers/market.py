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

@router.get("/sentiment")
async def get_market_sentiment(
    timeframe: str = "24h",
    db=Depends(get_db),
) -> Dict[str, Any]:
    """
    Aggregate sentiment scores from social sources and trading activity.
    Return trending topics, top players from recent trades, and AI market insights.
    """
    try:
        from datetime import datetime, timedelta

        # Map timeframe to hours
        timeframe_hours = {
            "1h": 1,
            "6h": 6,
            "24h": 24,
            "7d": 168
        }.get(timeframe, 24)

        cutoff = datetime.utcnow() - timedelta(hours=timeframe_hours)

        # Get top trending players from recent trades (proxy for market activity)
        trending_players = await db.fetch(
            """
            SELECT
                player,
                COUNT(*) as trade_count,
                SUM(quantity) as total_volume,
                AVG(profit) as avg_profit,
                SUM(CASE WHEN profit > 0 THEN 1 ELSE 0 END)::FLOAT / COUNT(*)::FLOAT as win_rate
            FROM trades
            WHERE timestamp >= $1
            GROUP BY player
            ORDER BY trade_count DESC
            LIMIT 10
            """,
            cutoff
        )

        # Calculate overall market sentiment
        total_trades = await db.fetchval(
            "SELECT COUNT(*) FROM trades WHERE timestamp >= $1",
            cutoff
        ) or 0

        profitable_trades = await db.fetchval(
            "SELECT COUNT(*) FROM trades WHERE timestamp >= $1 AND profit > 0",
            cutoff
        ) or 0

        # Sentiment score (0-100)
        if total_trades > 0:
            profit_ratio = profitable_trades / total_trades
            sentiment_score = int(profit_ratio * 100)
        else:
            sentiment_score = 50  # Neutral

        # Determine sentiment label
        if sentiment_score >= 70:
            sentiment_label = "Bullish"
            sentiment_icon = "🚀"
        elif sentiment_score >= 55:
            sentiment_label = "Positive"
            sentiment_icon = "📈"
        elif sentiment_score >= 45:
            sentiment_label = "Neutral"
            sentiment_icon = "➖"
        elif sentiment_score >= 30:
            sentiment_label = "Negative"
            sentiment_icon = "📉"
        else:
            sentiment_label = "Bearish"
            sentiment_icon = "🔻"

        # Generate AI insights based on data
        insights = []

        if total_trades > 100:
            insights.append(f"High trading activity detected ({total_trades} trades in {timeframe})")
        elif total_trades < 20:
            insights.append(f"Low market activity - consider waiting for better opportunities")

        if sentiment_score >= 65:
            insights.append("Strong buyer confidence. Good time to sell into demand.")
        elif sentiment_score <= 35:
            insights.append("Weak market conditions. Look for value buys or stay liquid.")

        # Top 3 trending players
        top_players = []
        for idx, p in enumerate(trending_players[:3]):
            win_rate = (p["win_rate"] or 0) * 100
            top_players.append({
                "rank": idx + 1,
                "player": p["player"],
                "trade_count": p["trade_count"],
                "volume": p["total_volume"],
                "avg_profit": int(p["avg_profit"] or 0),
                "win_rate": round(win_rate, 1)
            })

            if idx == 0:  # Top player insight
                if win_rate > 60:
                    insights.append(f"{p['player']} showing strong profit potential ({win_rate:.0f}% win rate)")
                elif win_rate < 40:
                    insights.append(f"{p['player']} is risky - only {win_rate:.0f}% of traders profiting")

        # Market timing insight
        hour = datetime.utcnow().hour
        if 18 <= hour <= 22:
            insights.append("Peak trading hours - high liquidity and faster sales")
        elif 2 <= hour <= 8:
            insights.append("Off-peak hours - fewer buyers, better deals available")

        return {
            "ok": True,
            "timeframe": timeframe,
            "sentiment": {
                "score": sentiment_score,
                "label": sentiment_label,
                "icon": sentiment_icon
            },
            "market_stats": {
                "total_trades": total_trades,
                "profitable_trades": profitable_trades,
                "win_rate": round(profit_ratio * 100, 1) if total_trades > 0 else 0
            },
            "trending_players": top_players,
            "insights": insights,
            "timestamp": datetime.utcnow().isoformat()
        }

    except Exception as e:
        import logging
        logging.error(f"market-sentiment error: {e}")
        raise HTTPException(status_code=500, detail=f"Market sentiment failed: {str(e)}")
