from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.auth.entitlements import compute_entitlements

router = APIRouter(prefix="/trades", tags=["v2-trades"])


class OpenTrade(BaseModel):
    card_id: Optional[int] = None
    player: str = Field(..., min_length=1)
    version: str = "Standard"
    buy: int = Field(..., gt=0)
    quantity: int = Field(1, ge=1, le=100)
    platform: str = "ps"
    bought_at: Optional[datetime] = None
    target_sell: Optional[int] = Field(None, gt=0)
    notes: str = ""


class CloseTrade(BaseModel):
    sell: int = Field(..., gt=0)
    sold_at: Optional[datetime] = None


async def _user_id(request: Request) -> str:
    ent = await compute_entitlements(request)
    user_id = ent.get("user_id")
    if not user_id:
        raise HTTPException(401, "Not authenticated")
    return str(user_id)


def _pool(request: Request):
    pool = getattr(request.app.state, "db_pool", None) or getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(503, "database pool not ready")
    return pool


@router.post("/open")
async def open_trade(request: Request, trade: OpenTrade):
    user_id = await _user_id(request)
    bought_at = trade.bought_at or datetime.now(timezone.utc)
    trade_id = int(bought_at.timestamp() * 1000)
    async with _pool(request).acquire() as conn:
        await conn.execute(
            """INSERT INTO trades (
                user_id, player, version, buy, sell, quantity, platform,
                profit, ea_tax, notes, timestamp, trade_id, card_id,
                bought_at, sold_at, status
            ) VALUES ($1,$2,$3,$4,NULL,$5,$6,0,0,$7,$8,$9,$10,$8,NULL,'open')""",
            user_id, trade.player.strip(), trade.version, trade.buy,
            trade.quantity, trade.platform, trade.notes or None,
            bought_at, trade_id, trade.card_id,
        )
    return {"ok": True, "trade_id": trade_id, "status": "open", "target_sell": trade.target_sell}


@router.patch("/{trade_id}/close")
async def close_trade(trade_id: int, request: Request, body: CloseTrade):
    user_id = await _user_id(request)
    sold_at = body.sold_at or datetime.now(timezone.utc)
    async with _pool(request).acquire() as conn:
        row = await conn.fetchrow(
            "SELECT buy, quantity FROM trades WHERE user_id=$1 AND trade_id=$2 AND status='open'",
            user_id, trade_id,
        )
        if not row:
            raise HTTPException(404, "Open trade not found")
        tax_each = body.sell - int(body.sell * 0.95)
        profit = (int(body.sell * 0.95) - row["buy"]) * row["quantity"]
        await conn.execute(
            """UPDATE trades SET sell=$3, sold_at=$4, status='closed',
                profit=$5, ea_tax=$6 WHERE user_id=$1 AND trade_id=$2""",
            user_id, trade_id, body.sell, sold_at, profit, tax_each * row["quantity"],
        )
    return {"ok": True, "trade_id": trade_id, "status": "closed", "profit": profit}


@router.get("/open")
async def list_open_trades(request: Request):
    user_id = await _user_id(request)
    async with _pool(request).acquire() as conn:
        rows = await conn.fetch(
            """SELECT * FROM trades WHERE user_id=$1 AND status='open'
            ORDER BY bought_at DESC NULLS LAST""", user_id,
        )
    return {"trades": [dict(row) for row in rows], "count": len(rows)}
