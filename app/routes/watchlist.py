
from __future__ import annotations
from typing import Dict, Any, List, Optional
from fastapi import APIRouter, HTTPException

router = APIRouter()

def _pool(request):
    pool = getattr(request.app.state, "pool", None)
    if pool is None: raise HTTPException(500, "DB pool not ready")
    return pool

@router.get("/api/watchlist")
async def list_watchlist(request, player_id: Optional[int] = None) -> List[Dict[str,Any]]:
    pool = _pool(request)
    uid = request.session.get("user_id") or request.headers.get("X-User-Id")
    if not uid: raise HTTPException(401, "No user")
    q = "SELECT * FROM watchlist_items WHERE user_id=$1 ORDER BY created_at DESC"
    params = [uid]
    if player_id:
        q = "SELECT * FROM watchlist_items WHERE user_id=$1 AND player_id=$2 ORDER BY created_at DESC"
        params.append(player_id)
    async with pool.acquire() as con:
        rows = await con.fetch(q, *params)
    return [dict(r) for r in rows]

@router.post("/api/watchlist")
async def add_watchlist(request, payload: Dict[str,Any]) -> Dict[str,Any]:
    pool = _pool(request)
    uid = request.session.get("user_id") or request.headers.get("X-User-Id")
    if not uid: raise HTTPException(401, "No user")
    for k in ("player_id","platform"):
        if k not in payload: raise HTTPException(400, f"missing {k}")
    async with pool.acquire() as con:
        await con.execute(
            "INSERT INTO watchlist_items (user_id, user_discord_id, player_id, platform, ref_mode, ref_price, rise_pct, fall_pct, cooloff_minutes, quiet_start, quiet_end, prefer_dm, fallback_channel_id) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)",
            uid, payload.get("user_discord_id"), int(payload["player_id"]), payload["platform"],
            payload.get("ref_mode","last_close"), payload.get("ref_price"), payload.get("rise_pct",5),
            payload.get("fall_pct",5), payload.get("cooloff_minutes",30), payload.get("quiet_start"),
            payload.get("quiet_end"), payload.get("prefer_dm", True), payload.get("fallback_channel_id")
        )
    return {"ok": True}
