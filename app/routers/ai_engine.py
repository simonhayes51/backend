# app/routers/ai_engine.py
from fastapi import APIRouter, Depends
from typing import Dict, Any
from app.db import get_db

router = APIRouter(prefix="/api/ai", tags=["AI Engine"])

def _pct(a: float, b: float) -> float:
    return (a - b) / b if b else 0.0

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