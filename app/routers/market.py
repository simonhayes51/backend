# app/routers/market.py 
from fastapi import APIRouter, Depends, HTTPException
from typing import List, Dict, Any
from app.db import get_player_db
from app.services.indicators import ema, rsi, bollinger, atr

router = APIRouter(prefix="/api/market", tags=["Market"])

# REPLACE the /now endpoint in app/routers/market.py with this:

@router.get("/now")
async def get_current_price(
    player_card_id: str,
    platform: str = "ps",
    db=Depends(get_player_db),
) -> Dict[str, Any]:
    """Get current market price for a player"""
    try:
        # Get from fut_players table
        player_row = await db.fetchrow(
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
    db=Depends(get_player_db),
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
    db=Depends(get_player_db),
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
    db=Depends(get_player_db),
) -> Dict[str, Any]:
    """
    Market-wide sentiment computed from real completed sales across the
    whole tracked Gold Rare population (bin_history/sales_history), not
    this app's own users' logged trades - that was a tiny, self-selected,
    gameable sample. Compares each card's median sold price in the recent
    window to the equal-length window right before it (advance/decline
    breadth, same idea stock market breadth indicators use) rather than a
    simple win/loss ratio on personal trade logs.
    """
    try:
        from datetime import datetime

        timeframe_hours = {"1h": 1, "6h": 6, "24h": 24, "7d": 168}.get(timeframe, 24)

        rows = await db.fetch(
            """
            WITH recent AS (
                SELECT player_id,
                       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY sold_price) AS median_recent,
                       COUNT(*) AS n_recent
                FROM sales_history
                WHERE sold_at >= NOW() - make_interval(hours => $1)
                GROUP BY player_id
            ),
            prior AS (
                SELECT player_id,
                       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY sold_price) AS median_prior,
                       COUNT(*) AS n_prior
                FROM sales_history
                WHERE sold_at >= NOW() - make_interval(hours => $1 * 2)
                  AND sold_at < NOW() - make_interval(hours => $1)
                GROUP BY player_id
            )
            SELECT recent.player_id, median_recent, n_recent, median_prior, n_prior
            FROM recent
            JOIN prior ON prior.player_id = recent.player_id
            WHERE recent.n_recent >= 3 AND prior.n_prior >= 3
            """,
            timeframe_hours,
        )

        total_recent_sales = await db.fetchval(
            "SELECT COUNT(*) FROM sales_history WHERE sold_at >= NOW() - make_interval(hours => $1)",
            timeframe_hours,
        ) or 0
        total_prior_sales = await db.fetchval(
            """
            SELECT COUNT(*) FROM sales_history
            WHERE sold_at >= NOW() - make_interval(hours => $1 * 2)
              AND sold_at < NOW() - make_interval(hours => $1)
            """,
            timeframe_hours,
        ) or 0

        movers = []
        risers = 0
        fallers = 0
        for r in rows:
            median_recent = float(r["median_recent"])
            median_prior = float(r["median_prior"])
            if median_prior <= 0:
                continue
            pct_change = round((median_recent - median_prior) / median_prior * 100, 2)
            if pct_change > 1:
                risers += 1
            elif pct_change < -1:
                fallers += 1
            movers.append({
                "player_id": int(r["player_id"]),
                "medianRecent": round(median_recent),
                "medianPrior": round(median_prior),
                "pctChange": pct_change,
                "salesRecent": r["n_recent"],
            })

        qualifying = risers + fallers
        sentiment_score = round(risers / qualifying * 100) if qualifying > 0 else 50

        if sentiment_score >= 70:
            sentiment_label, sentiment_icon = "Bullish", "🚀"
        elif sentiment_score >= 55:
            sentiment_label, sentiment_icon = "Positive", "📈"
        elif sentiment_score >= 45:
            sentiment_label, sentiment_icon = "Neutral", "➖"
        elif sentiment_score >= 30:
            sentiment_label, sentiment_icon = "Negative", "📉"
        else:
            sentiment_label, sentiment_icon = "Bearish", "🔻"

        volume_change_pct = (
            round((total_recent_sales - total_prior_sales) / total_prior_sales * 100, 1)
            if total_prior_sales > 0 else None
        )

        insights = [
            f"{risers} cards rising vs {fallers} falling across {qualifying} tracked Gold Rare cards "
            f"with enough sales to compare over the last {timeframe}."
        ]
        if volume_change_pct is not None:
            direction = "up" if volume_change_pct > 0 else "down" if volume_change_pct < 0 else "flat"
            insights.append(
                f"Completed sales volume is {direction} {abs(volume_change_pct)}% vs the previous "
                f"{timeframe} window ({total_recent_sales} vs {total_prior_sales} sales)."
            )
        if sentiment_score >= 65:
            insights.append("Broad-based buying - prices rising across more cards than they're falling.")
        elif sentiment_score <= 35:
            insights.append("Broad-based selling pressure - more cards falling than rising right now.")

        movers.sort(key=lambda m: m["pctChange"], reverse=True)
        top_risers = movers[:3]
        top_fallers = sorted(movers, key=lambda m: m["pctChange"])[:3]

        mover_ids = [m["player_id"] for m in top_risers + top_fallers]
        names: Dict[int, Any] = {}
        if mover_ids:
            name_rows = await db.fetch(
                "SELECT card_id, name, rating, image_url FROM fut_players WHERE card_id = ANY($1::bigint[])",
                mover_ids,
            )
            names = {int(r["card_id"]): dict(r) for r in name_rows}

        def _enrich(m: Dict[str, Any]) -> Dict[str, Any]:
            meta = names.get(m["player_id"], {})
            return {
                **m,
                "name": meta.get("name"),
                "rating": meta.get("rating"),
                "image_url": meta.get("image_url"),
            }

        return {
            "ok": True,
            "timeframe": timeframe,
            "sentiment": {
                "score": sentiment_score,
                "label": sentiment_label,
                "icon": sentiment_icon,
            },
            "market_stats": {
                "cards_rising": risers,
                "cards_falling": fallers,
                "cards_compared": qualifying,
                "total_sales_recent": total_recent_sales,
                "total_sales_prior": total_prior_sales,
                "volume_change_pct": volume_change_pct,
            },
            "top_risers": [_enrich(m) for m in top_risers],
            "top_fallers": [_enrich(m) for m in top_fallers],
            "insights": insights,
            "timestamp": datetime.utcnow().isoformat(),
        }

    except Exception as e:
        import logging
        logging.error(f"market-sentiment error: {e}")
        raise HTTPException(status_code=500, detail=f"Market sentiment failed: {str(e)}")
