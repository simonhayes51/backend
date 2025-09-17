# app/routers/ai_engine.py 
from fastapi import APIRouter, Depends
from typing import Dict, Any
from app.db import get_db

router = APIRouter(prefix="/api/ai", tags=["AI Engine"])

def _pct(a: float, b: float) -> float:
    return (a - b) / b if b else 0.0

# Add this to app/routers/ai_engine.py

# REPLACE the top-buys endpoint in app/routers/ai_engine.py with this:

@router.get("/top-buys")
async def top_buys(
    platform: str = "ps",
    limit: int = 36,
    db=Depends(get_db),
):
    """
    Get top buy recommendations with risk assessment
    """
    try:
        # Import here to avoid circular imports
        from main import app
        
        async with app.state.player_pool.acquire() as pconn:
            rows = await pconn.fetch(
                """
                SELECT 
                    card_id,
                    name,
                    rating,
                    position,
                    version,
                    image_url,
                    price,
                    price_num,
                    club,
                    league,
                    nation
                FROM fut_players 
                WHERE (
                    (price_num IS NOT NULL AND price_num > 1000 AND price_num < 2000000)
                    OR 
                    (price IS NOT NULL AND price ~ '^[0-9]+$' AND price::integer > 1000 AND price::integer < 2000000)
                )
                AND rating >= 75
                ORDER BY 
                    rating DESC,
                    COALESCE(price_num, CASE WHEN price ~ '^[0-9]+$' THEN price::integer ELSE 999999999 END) ASC
                LIMIT $1
                """, 
                limit
            )
        
        results = []
        for row in rows:
            # Get the actual price value
            if row["price_num"]:
                current_price = int(row["price_num"])
            elif row["price"] and str(row["price"]).isdigit():
                current_price = int(row["price"])
            else:
                continue  # Skip if no valid price
            
            # Mock some analysis data based on price patterns
            card_id_int = int(row["card_id"])
            price_variance = (card_id_int % 100) / 1000  # Mock variance 0-0.1
            
            # Simulate median price (slightly different from current)
            median7 = int(current_price * (0.95 + price_variance))
            
            # Calculate percentage difference
            if median7 > 0:
                cheap_pct = (current_price - median7) / median7
            else:
                cheap_pct = 0
            
            # Mock volume (based on rating and price)
            vol24 = max(10, (row["rating"] or 75) - 50 + (card_id_int % 30))
            
            # Determine risk level
            abs_cheap_pct = abs(cheap_pct)
            if abs_cheap_pct < 0.05:
                risk_label = "Low"
            elif abs_cheap_pct < 0.15:
                risk_label = "Medium"
            else:
                risk_label = "High"
            
            results.append({
                "player_card_id": str(row["card_id"]),  # Use card_id, not player_card_id
                "player": {
                    "name": row["name"],
                    "rating": row["rating"],
                    "position": row["position"],
                    "version": row["version"] or "Standard",
                    "image_url": row["image_url"]
                },
                "current": current_price,
                "median7": median7,
                "cheap_pct": cheap_pct,
                "vol24": vol24,
                "risk_label": risk_label
            })
        
        return results
        
    except Exception as e:
        import logging
        import traceback
        logging.error(f"top-buys error: {e}")
        logging.error(f"traceback: {traceback.format_exc()}")
        return {"ok": False, "reason": f"top-buys failed: {e}", "data": []}

@router.get("/recommendations")
async def recommendations(
    player_card_id: str,
    platform: str = "ps",
    db=Depends(get_db),
) -> Dict[str, Any]:
    try:
        rows = await db.fetch(
            """
            SELECT open_time, close
            FROM public.fut_candles
            WHERE player_card_id=$1 AND platform=$2 AND timeframe='15m'
            ORDER BY open_time ASC
            """,
            player_card_id, platform,
        )
        data = [dict(r) for r in rows]
        if len(data) < 20:
            return {"ok": False, "reason": "insufficient candles (need >= 20 x 15m)", "have": len(data)}

        closes = [float(d["close"]) for d in data]
        curr = closes[-1]
        srt = sorted(closes)
        n = len(srt)
        med7 = (srt[n//2] if n % 2 else (srt[n//2-1] + srt[n//2]) / 2)

        dmed = _pct(curr, med7)
        lookback = 12 if len(closes) > 12 else max(len(closes)-1, 1)
        ch3h = _pct(curr, closes[-lookback]) if lookback > 0 else 0.0

        action, tgt, stop, comment = "HOLD", None, None, "No clear edge."
        if dmed <= -0.05:
            action = "BUY"
            tgt = int(max(med7 * 0.99, curr * 1.05))
            stop = int(curr * 0.97)
            comment = f"Value: now {dmed*100:.1f}% under 7D median ({int(med7):,})."
        elif ch3h >= 0.08:
            action = "AVOID_OR_SELL"
            comment = f"Momentum: +{ch3h*100:.1f}% in ~3h. Likely hype."

        return {
            "ok": True, "player_card_id": player_card_id, "platform": platform,
            "current": int(curr), "median7": int(med7),
            "delta_to_median": round(dmed, 4), "change_3h": round(ch3h, 4),
            "action": action, "target_sell": tgt, "stop_loss": stop, "comment": comment,
        }
    except Exception as e:
        return {"ok": False, "reason": f"recommendations failed: {e}"}
