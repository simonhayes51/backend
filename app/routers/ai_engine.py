# app/routers/ai_engine.py 
from fastapi import APIRouter, Depends
from typing import Dict, Any
from app.db import get_db

router = APIRouter(prefix="/api/ai", tags=["AI Engine"])

def _pct(a: float, b: float) -> float:
    return (a - b) / b if b else 0.0

# Add this to app/routers/ai_engine.py

@router.get("/top-buys")
async def top_buys(
    platform: str = "ps",
    limit: int = 36,
    db=Depends(get_db),
) -> Dict[str, Any]:
    """
    Get top buy recommendations with risk assessment
    """
    try:
        # This would need to be implemented based on your logic
        # For now, returning a placeholder structure that matches your frontend expectations
        
        # Get some sample players with price data
        rows = await db.fetch(
            """
            SELECT 
                p.card_id as player_card_id,
                p.name,
                p.rating,
                p.position,
                p.version,
                p.image_url,
                p.price_num as current_price
            FROM fut_players p 
            WHERE p.price_num IS NOT NULL 
            AND p.price_num > 1000 
            AND p.price_num < 500000
            ORDER BY p.rating DESC 
            LIMIT $1
            """, 
            limit
        )
        
        results = []
        for row in rows:
            # Mock some analysis data - you'll need to implement real logic here
            current = row["current_price"]
            median7 = int(current * (0.9 + (hash(str(row["card_id"])) % 100) / 500))  # Mock median
            cheap_pct = (current - median7) / median7 if median7 > 0 else 0
            vol24 = hash(str(row["card_id"])) % 50 + 10  # Mock volume
            
            # Determine risk level based on price volatility
            if abs(cheap_pct) < 0.05:
                risk_label = "Low"
            elif abs(cheap_pct) < 0.15:
                risk_label = "Medium" 
            else:
                risk_label = "High"
            
            results.append({
                "player_card_id": str(row["player_card_id"]),
                "player": {
                    "name": row["name"],
                    "rating": row["rating"],
                    "position": row["position"],
                    "version": row["version"] or "Standard",
                    "image_url": row["image_url"]
                },
                "current": current,
                "median7": median7,
                "cheap_pct": cheap_pct,
                "vol24": vol24,
                "risk_label": risk_label
            })
        
        return results
        
    except Exception as e:
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
