from fastapi import APIRouter, Depends, HTTPException
from typing import Dict, Any
from app.db import get_db

EA_TAX = 0.05
BASE_SPREAD = 0.009   # 0.9% round-trip modelled spread
SLIPPAGE = 0.003      # 0.3% slippage

router = APIRouter(prefix="/api/ai", tags=["AI Engine"])

def pct(a, b): return (a-b)/b if b else 0.0

@router.get("/recommendations")
async def recommendations(
    player_card_id: str,
    platform: str = "ps",
    db=Depends(get_db)
) -> Dict[str, Any]:
    rows = await db.fetch(
        """
        SELECT open_time, close
        FROM fut_candles
        WHERE player_card_id=$1 AND platform=$2 AND timeframe='15m'
        ORDER BY open_time DESC
        LIMIT 7*24*4
        """,
        player_card_id, platform,
    )
    data = [dict(r) for r in rows][::-1]
    if len(data) < 20:
        return {"ok": False, "reason": "insufficient data"}

    closes = [d["close"] for d in data]
    curr = closes[-1]
    med7 = sorted(closes)[len(closes)//2]
    dmed = pct(curr, med7)

    lb = 12 if len(closes) > 12 else len(closes)-1
    ch3h = pct(curr, closes[-lb]) if lb > 0 else 0

    action, tgt, stop, comment = "HOLD", None, None, "No clear edge."
    if dmed <= -0.05:
        action = "BUY"
        tgt = max(int(med7*0.99), int(curr*1.05))
        stop = int(curr*0.97)
        comment = f"Value: now {dmed*100:.1f}% under 7D median ({med7:,})."
    elif ch3h >= 0.08:
        action = "AVOID_OR_SELL"
        comment = f"Momentum: +{ch3h*100:.1f}% in ~3h. Likely hype."

    return {
        "ok": True,
        "player_card_id": player_card_id,
        "platform": platform,
        "current": curr,
        "median7": med7,
        "delta_to_median": round(dmed, 4),
        "change_3h": round(ch3h, 4),
        "action": action,
        "target_sell": tgt,
        "stop_loss": stop,
        "comment": comment
    }

@router.post("/signal")
async def place_signal(
    player_card_id: str,
    platform: str = "ps",
    agent: str = "Conservative",
    db=Depends(get_db)
):
    rec = await recommendations(player_card_id, platform, db)
    if not rec.get("ok") or rec["action"] != "BUY":
        raise HTTPException(400, "No BUY setup right now")

    await db.execute(
        """
        INSERT INTO ai_orders (player_card_id, platform, agent, side, type, qty, limit_price, comment)
        VALUES ($1,$2,$3,'BUY','LIMIT',1,$4,$5)
        """,
        player_card_id, platform, agent, rec["current"], rec["comment"],
    )
    return {"ok": True, "placed": rec}
