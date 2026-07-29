from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.auth.entitlements import compute_entitlements

router = APIRouter(prefix="/trades", tags=["v2-trades"])


class RecommendationSnapshot(BaseModel):
    status: Optional[str] = None
    strategy: Optional[str] = None
    confidence: Optional[float] = None
    expected_roi: Optional[float] = None
    buy_below: Optional[int] = None
    sell_around: Optional[int] = None
    fair_value: Optional[int] = None
    reasoning: Optional[str] = None


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
    recommendation: Optional[RecommendationSnapshot] = None


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
    snapshot: Optional[Dict[str, Any]] = trade.recommendation.model_dump() if trade.recommendation else None
    rec = trade.recommendation

    async with _pool(request).acquire() as conn:
        await conn.execute(
            """INSERT INTO trades (
                user_id, player, version, buy, sell, quantity, platform,
                profit, ea_tax, notes, timestamp, trade_id, card_id,
                bought_at, sold_at, status, target_sell,
                recommendation_status, recommendation_strategy,
                recommendation_confidence, recommendation_expected_roi,
                recommendation_buy_below, recommendation_sell_around,
                recommendation_fair_value, recommendation_snapshot
            ) VALUES (
                $1,$2,$3,$4,NULL,$5,$6,0,0,$7,$8,$9,$10,
                $11,NULL,'open',$12,$13,$14,$15,$16,$17,$18,$19,$20
            )""",
            user_id,
            trade.player.strip(),
            trade.version,
            trade.buy,
            trade.quantity,
            trade.platform,
            trade.notes or None,
            bought_at,
            trade_id,
            trade.card_id,
            bought_at,
            trade.target_sell,
            rec.status if rec else None,
            rec.strategy if rec else None,
            rec.confidence if rec else None,
            rec.expected_roi if rec else None,
            rec.buy_below if rec else None,
            rec.sell_around if rec else None,
            rec.fair_value if rec else None,
            snapshot,
        )
    return {"ok": True, "trade_id": trade_id, "status": "open", "target_sell": trade.target_sell}


@router.patch("/{trade_id}/close")
async def close_trade(trade_id: int, request: Request, body: CloseTrade):
    user_id = await _user_id(request)
    sold_at = body.sold_at or datetime.now(timezone.utc)
    async with _pool(request).acquire() as conn:
        row = await conn.fetchrow(
            "SELECT buy, quantity FROM trades WHERE user_id=$1 AND trade_id=$2 AND status='open'",
            user_id,
            trade_id,
        )
        if not row:
            raise HTTPException(404, "Open trade not found")
        tax_each = body.sell - int(body.sell * 0.95)
        profit = (int(body.sell * 0.95) - row["buy"]) * row["quantity"]
        await conn.execute(
            """UPDATE trades SET sell=$3, sold_at=$4, status='closed',
                profit=$5, ea_tax=$6 WHERE user_id=$1 AND trade_id=$2""",
            user_id,
            trade_id,
            body.sell,
            sold_at,
            profit,
            tax_each * row["quantity"],
        )
    return {"ok": True, "trade_id": trade_id, "status": "closed", "profit": profit}


@router.get("/open")
async def list_open_trades(request: Request):
    user_id = await _user_id(request)
    async with _pool(request).acquire() as conn:
        rows = await conn.fetch(
            """SELECT * FROM trades WHERE user_id=$1 AND status='open'
            ORDER BY bought_at DESC NULLS LAST""",
            user_id,
        )
    return {"trades": [dict(row) for row in rows], "count": len(rows)}


@router.get("/history")
async def trade_history(request: Request, limit: int = Query(100, ge=1, le=500)):
    user_id = await _user_id(request)
    async with _pool(request).acquire() as conn:
        rows = await conn.fetch(
            """SELECT * FROM trades WHERE user_id=$1 AND status='closed'
            ORDER BY sold_at DESC NULLS LAST LIMIT $2""",
            user_id,
            limit,
        )
    return {"trades": [dict(row) for row in rows], "count": len(rows)}


@router.get("/profit-timeline")
async def profit_timeline(request: Request, days: int = Query(30, ge=7, le=365)):
    user_id = await _user_id(request)
    async with _pool(request).acquire() as conn:
        rows = await conn.fetch(
            """WITH dates AS (
                SELECT generate_series(
                    CURRENT_DATE - ($2::int - 1), CURRENT_DATE, INTERVAL '1 day'
                )::date AS day
            ), daily AS (
                SELECT sold_at::date AS day, SUM(profit)::bigint AS profit
                FROM trades
                WHERE user_id=$1 AND status='closed'
                  AND sold_at >= CURRENT_DATE - ($2::int - 1)
                GROUP BY sold_at::date
            )
            SELECT dates.day, COALESCE(daily.profit, 0)::bigint AS profit,
                   SUM(COALESCE(daily.profit, 0)) OVER (ORDER BY dates.day)::bigint AS cumulative_profit
            FROM dates LEFT JOIN daily USING(day)
            ORDER BY dates.day""",
            user_id,
            days,
        )
    return {"items": [dict(row) for row in rows]}


@router.get("/community/{card_id}")
async def card_community(card_id: int, request: Request):
    await _user_id(request)
    async with _pool(request).acquire() as conn:
        row = await conn.fetchrow(
            """SELECT
                COUNT(DISTINCT user_id) AS users_traded,
                COUNT(*) FILTER (WHERE status='open') AS currently_holding,
                COUNT(*) FILTER (WHERE status='closed') AS already_sold,
                ROUND(AVG(buy))::bigint AS average_buy,
                ROUND(AVG(sell) FILTER (WHERE status='closed'))::bigint AS average_sell
            FROM trades WHERE card_id=$1""",
            card_id,
        )
    data = dict(row or {})
    total = int(data.get("currently_holding") or 0) + int(data.get("already_sold") or 0)
    data["holding_percent"] = round((int(data.get("currently_holding") or 0) / total) * 100, 1) if total else 0
    data["sold_percent"] = round((int(data.get("already_sold") or 0) / total) * 100, 1) if total else 0
    return data


@router.get("/performance")
async def trading_performance(request: Request):
    user_id = await _user_id(request)
    async with _pool(request).acquire() as conn:
        summary = await conn.fetchrow(
            """
            SELECT
              COUNT(*) FILTER (WHERE status='open') AS open_positions,
              COUNT(*) FILTER (WHERE status='closed') AS closed_trades,
              COUNT(*) FILTER (WHERE status='closed' AND profit > 0) AS wins,
              COALESCE(SUM(profit) FILTER (WHERE status='closed'), 0) AS total_profit,
              COALESCE(SUM(profit) FILTER (WHERE status='closed' AND sold_at >= NOW() - INTERVAL '1 day'), 0) AS profit_today,
              COALESCE(SUM(profit) FILTER (WHERE status='closed' AND sold_at >= NOW() - INTERVAL '7 days'), 0) AS profit_week,
              COALESCE(SUM(ea_tax) FILTER (WHERE status='closed'), 0) AS total_ea_tax,
              COALESCE(AVG(EXTRACT(EPOCH FROM (sold_at - bought_at)) / 3600.0)
                FILTER (WHERE status='closed' AND sold_at IS NOT NULL AND bought_at IS NOT NULL), 0) AS average_hold_hours,
              MAX(profit) FILTER (WHERE status='closed') AS best_profit,
              MIN(profit) FILTER (WHERE status='closed') AS worst_profit,
              COALESCE(AVG((profit::numeric / NULLIF(buy * quantity, 0)) * 100)
                FILTER (WHERE status='closed'), 0) AS average_roi
            FROM trades
            WHERE user_id=$1
            """,
            user_id,
        )
        strategies = await conn.fetch(
            """
            SELECT COALESCE(recommendation_strategy, 'Unlabelled') AS strategy,
              COUNT(*) AS trades, COUNT(*) FILTER (WHERE profit > 0) AS wins,
              COALESCE(SUM(profit), 0) AS profit,
              COALESCE(AVG((profit::numeric / NULLIF(buy * quantity, 0)) * 100), 0) AS roi
            FROM trades WHERE user_id=$1 AND status='closed'
            GROUP BY COALESCE(recommendation_strategy, 'Unlabelled') ORDER BY profit DESC
            """,
            user_id,
        )
        confidence = await conn.fetch(
            """
            SELECT CASE WHEN recommendation_confidence >= 80 THEN '80-100'
                WHEN recommendation_confidence >= 60 THEN '60-79'
                WHEN recommendation_confidence >= 40 THEN '40-59' ELSE '0-39' END AS band,
              COUNT(*) AS trades, COUNT(*) FILTER (WHERE profit > 0) AS wins,
              COALESCE(AVG(profit), 0) AS average_profit
            FROM trades WHERE user_id=$1 AND status='closed' AND recommendation_confidence IS NOT NULL
            GROUP BY 1 ORDER BY 1 DESC
            """,
            user_id,
        )

    data = dict(summary or {})
    closed = int(data.get("closed_trades") or 0)
    wins = int(data.get("wins") or 0)
    data["win_rate"] = round((wins / closed) * 100, 1) if closed else 0
    data["strategies"] = [dict(row) for row in strategies]
    data["confidence_accuracy"] = [dict(row) for row in confidence]
    return data
