from __future__ import annotations

from typing import Dict, Any, List, Optional
from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


def _pool(request: Request):
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(500, "DB pool not ready")
    return pool


def _uid_int(request: Request) -> int:
    # session/header values are strings -> asyncpg wants int for BIGINT
    uid = None
    try:
        uid = request.session.get("user_id")  # type: ignore[attr-defined]
    except Exception:
        uid = None

    if uid is None:
        uid = request.headers.get("X-User-Id")

    if uid is None:
        raise HTTPException(401, "No user")

    try:
        return int(uid)
    except (TypeError, ValueError):
        raise HTTPException(400, f"Invalid user id: {uid}")


def _int_or_none(v: Any) -> Optional[int]:
    if v is None:
        return None
    if isinstance(v, int):
        return v
    s = str(v).strip()
    if s == "":
        return None
    try:
        return int(s)
    except ValueError:
        raise HTTPException(400, f"Invalid integer: {v}")


@router.get("/api/watchlist")
async def list_watchlist(request: Request, card_id: Optional[int] = None) -> List[Dict[str, Any]]:
    pool = _pool(request)
    uid = _uid_int(request)  # ✅ int, not str

    q = "SELECT * FROM watchlist WHERE user_id=$1 ORDER BY started_at DESC"
    params: List[Any] = [uid]

    if card_id is not None:
        q = "SELECT * FROM watchlist WHERE user_id=$1 AND card_id=$2 ORDER BY started_at DESC"
        params.append(int(card_id))

    async with pool.acquire() as con:
        rows = await con.fetch(q, *params)

    return [dict(r) for r in rows]


@router.post("/api/watchlist")
async def add_watchlist(request: Request, payload: Dict[str, Any]) -> Dict[str, Any]:
    pool = _pool(request)
    uid = _uid_int(request)  # ✅ int, not str

    # Expecting at least card_id + platform based on your table screenshot
    for k in ("card_id", "platform"):
        if k not in payload:
            raise HTTPException(400, f"missing {k}")

    card_id = _int_or_none(payload.get("card_id"))
    if card_id is None:
        raise HTTPException(400, "missing card_id")

    # Optional fields (adjust to your real columns)
    user_discord_id = _int_or_none(payload.get("user_discord_id"))

    async with pool.acquire() as con:
        await con.execute(
            """
            INSERT INTO watchlist
            (user_id, user_discord_id, card_id, platform, ref_mode, ref_price, rise_pct, fall_pct,
             cooloff_minutes, quiet_start, quiet_end, prefer_dm, fallback_channel_id)
            VALUES
            ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
            """,
            uid,
            user_discord_id,
            card_id,
            payload["platform"],
            payload.get("ref_mode", "last_close"),
            payload.get("ref_price"),
            payload.get("rise_pct", 5),
            payload.get("fall_pct", 5),
            payload.get("cooloff_minutes", 30),
            payload.get("quiet_start"),
            payload.get("quiet_end"),
            payload.get("prefer_dm", True),
            _int_or_none(payload.get("fallback_channel_id")),
        )

    return {"ok": True}
