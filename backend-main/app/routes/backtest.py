
from __future__ import annotations
from typing import Dict, Any, List
from fastapi import APIRouter, HTTPException
from app.services.price_history import get_price_history

router = APIRouter()

def _series(hist: List[dict]):
    out = []
    for p in hist:
        t = p.get("t") or p.get("ts") or p.get("time")
        v = p.get("price") or p.get("v") or p.get("y")
        if t is not None and v is not None:
            out.append((int(t), float(v)))
    return out

@router.post("/api/backtest")
async def backtest(payload: Dict[str, Any]) -> Dict[str, Any]:
    players = payload.get("players") or []
    platform = payload.get("platform", "ps")
    window_days = int(payload.get("window_days", 7))
    entry = payload.get("entry", {"type":"dip_from_high","x_pct":5})
    exit_ = payload.get("exit", {"tp_pct":7, "sl_pct":4, "max_hold_h":24})
    size = payload.get("size", {"coins":200000})
    concurrency = int(payload.get("concurrency", 3))

    if not players: raise HTTPException(400, "players required")

    equity = []
    all_trades = []
    cash = 0.0
    open_trades = []

    for pid in players:
        hist = await get_price_history(player_id=pid, platform=platform, tf="today")
        pts = _series(hist)[-(window_days*96):]
        if len(pts) < 16: continue
        recent_high = max(v for _,v in pts[:8])
        for i in range(8, len(pts)):
            t, px = pts[i]
            if px > recent_high: recent_high = px
            # exits
            keep = []
            for tr in open_trades:
                tp = tr["entry_price"] * (1 + exit_.get("tp_pct",7)/100.0)
                sl = tr["entry_price"] * (1 - exit_.get("sl_pct",4)/100.0)
                hold_h = (t - tr["t_in"]) / 3600000.0
                reason = None
                if px >= tp: reason = "tp"
                elif px <= sl: reason = "sl"
                elif hold_h >= exit_.get("max_hold_h",24): reason = "time"
                if reason:
                    pnl = (px - tr["entry_price"]) * tr["qty"]
                    pnl_after_tax = pnl * 0.95
                    all_trades.append({**tr, "t_out": t, "px_out": px, "exit": reason, "pnl_after_tax": pnl_after_tax})
                    cash += pnl_after_tax
                else:
                    keep.append(tr)
            open_trades = keep

            if len(open_trades) < concurrency and entry.get("type") == "dip_from_high":
                x = entry.get("x_pct", 5)
                if recent_high > 0 and ((recent_high - px)/recent_high)*100 >= x:
                    qty = max(1, int(size.get("coins",200000) // px))
                    open_trades.append({"player_id": pid, "t_in": t, "px_in": px, "entry_price": px, "qty": qty})

            equity.append({"t": t, "value": cash + sum((px - tr["entry_price"]) * tr["qty"] * 0.95 for tr in open_trades)})

    wins = [tr for tr in all_trades if tr["pnl_after_tax"] > 0]
    summary = {
        "trades": len(all_trades),
        "net_profit": round(sum(tr["pnl_after_tax"] for tr in all_trades), 2),
        "win_rate": round(100*len(wins)/len(all_trades), 1) if all_trades else 0.0,
        "avg_hold_h": round(sum(((tr["t_out"]-tr["t_in"])/3600000.0) for tr in all_trades)/len(all_trades), 2) if all_trades else 0.0,
    }
    return {"equity": equity, "summary": summary, "trades": all_trades}
