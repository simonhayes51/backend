# app/routers/public_api_v2.py
"""
Public Data API v2 — the licensable product tier over FUTHub's proprietary
market data. Everything here is API-key authed (X-API-Key) with per-minute
rate limits and hard monthly quotas per key tier (see app/auth/api_keys.py):

    starter   60 rpm     5,000 req/mo   free taster (self-serve, Pro account)
    trader   120 rpm   100,000 req/mo   £19/mo
    dev      600 rpm 2,000,000 req/mo   £79/mo

v2 adds over v1:
  - /fair-value/{card_id}  precomputed real-sales fair value + discount
  - /undervalued           the ranked mispricing board
  - /anomalies             statistical BIN outliers (snipe radar)
  - /players/{id}/candles  bucketed OHLC-ish series from real sales
  - /usage                 the key's own quota/usage introspection
plus everything v1 had (search, bin-history, sales-history, market-metrics).

v1 (/api/public/v1) stays frozen for existing consumers.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.auth.api_keys import require_api_key
from app.db import get_player_db, get_db
from app.services import fair_value as fv
from app.services.price_history import get_price_history

router = APIRouter(prefix="/api/public/v2", tags=["public-api-v2"])


def _player_pool(request: Request):
    pool = getattr(request.app.state, "player_pool", None)
    if pool is None:
        raise HTTPException(503, "player pool not ready")
    return pool


# ----------------------------- players ---------------------------------------


@router.get("/players/search")
async def v2_search_players(
    q: str = Query("", description="name or card_id substring"),
    limit: int = Query(50, le=200),
    key=Depends(require_api_key),
    conn=Depends(get_player_db),
):
    q = (q or "").strip()
    where = "TRUE"
    params: List[Any] = []
    if q:
        where = "(LOWER(name) LIKE LOWER($1) OR card_id::text LIKE $1)"
        params.append(f"%{q}%")
    params.append(limit)
    rows = await conn.fetch(
        f"""
        SELECT card_id, name, rating, version, club, league, nation, position, price_num
        FROM fut_players
        WHERE {where}
        ORDER BY rating DESC NULLS LAST
        LIMIT ${len(params)}
        """,
        *params,
    )
    return {"players": [dict(r) for r in rows]}


@router.get("/sbc/cheap-fodder")
async def v2_cheap_fodder(
    min_rating: int = Query(..., ge=1, le=99, description="SBC's minimum rating requirement"),
    max_rating: Optional[int] = Query(None, ge=1, le=99),
    position: Optional[str] = Query(None, description="exact position code, e.g. ST, CB, GK"),
    league: Optional[str] = Query(None, description="substring match"),
    nation: Optional[str] = Query(None, description="substring match"),
    limit: int = Query(20, ge=1, le=50),
    key=Depends(require_api_key),
    conn=Depends(get_player_db),
):
    """Cheapest currently-priced cards meeting an SBC's rating (and optional
    position/league/nation) requirement - a manual shopping list, not a
    solver. Consumers still pick, buy, and place cards themselves."""
    where = ["rating >= $1", "price_num IS NOT NULL", "price_num > 0"]
    params: List[Any] = [min_rating]
    if max_rating is not None:
        params.append(max_rating)
        where.append(f"rating <= ${len(params)}")
    if position:
        params.append(position.strip().upper())
        where.append(f"UPPER(position) = ${len(params)}")
    if league:
        params.append(f"%{league.strip()}%")
        where.append(f"LOWER(league) LIKE LOWER(${len(params)})")
    if nation:
        params.append(f"%{nation.strip()}%")
        where.append(f"LOWER(nation) LIKE LOWER(${len(params)})")
    params.append(limit)

    rows = await conn.fetch(
        f"""
        SELECT card_id, name, rating, version, position, club, league, nation, price_num
        FROM fut_players
        WHERE {' AND '.join(where)}
        ORDER BY price_num ASC
        LIMIT ${len(params)}
        """,
        *params,
    )
    return {"items": [dict(r) for r in rows], "count": len(rows)}


@router.get("/players/{card_id}/bin-history")
async def v2_bin_history(
    card_id: int,
    platform: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=2000),
    key=Depends(require_api_key),
    conn=Depends(get_player_db),
):
    where: List[str] = ["player_id = $1"]
    params: List[Any] = [card_id]
    if platform:
        params.append(platform.lower())
        where.append(f"platform = ${len(params)}")
    params.append(limit)
    rows = await conn.fetch(
        f"""
        SELECT platform, lowest_bin, captured_at
        FROM bin_history
        WHERE {' AND '.join(where)}
        ORDER BY captured_at DESC
        LIMIT ${len(params)}
        """,
        *params,
    )
    return {
        "card_id": card_id,
        "points": [
            {"platform": r["platform"], "lowestBin": r["lowest_bin"], "capturedAt": r["captured_at"].isoformat()}
            for r in rows
        ],
    }


@router.get("/players/{card_id}/sales-history")
async def v2_sales_history(
    card_id: int,
    limit: int = Query(50, ge=1, le=500),
    key=Depends(require_api_key),
    conn=Depends(get_player_db),
):
    rows = await conn.fetch(
        """
        SELECT listed_price, sold_price, ea_tax, net_price, sold_at
        FROM sales_history
        WHERE player_id = $1
        ORDER BY sold_at DESC
        LIMIT $2
        """,
        card_id, limit,
    )
    return {
        "card_id": card_id,
        "platform": "ps",
        "sales": [
            {
                "listedPrice": r["listed_price"],
                "soldPrice": r["sold_price"],
                "eaTax": r["ea_tax"],
                "netPrice": r["net_price"],
                "soldAt": r["sold_at"].isoformat(),
            }
            for r in rows
        ],
    }


@router.get("/players/{card_id}/candles")
async def v2_candles(
    card_id: int,
    tf: str = Query("today", pattern="^(today|3d|week|month|year)$"),
    platform: str = Query("ps"),
    key=Depends(require_api_key),
):
    """Bucketed median-sold series from real sales (same series the app's
    charts use), as a structured feed instead of HTML scraping."""
    hist = await get_price_history(card_id, platform, tf)
    return {"card_id": card_id, "tf": tf, "points": (hist or {}).get("points") or []}


@router.get("/players/{card_id}/market-metrics")
async def v2_market_metrics(
    card_id: int,
    key=Depends(require_api_key),
    conn=Depends(get_player_db),
):
    bin_rows = await conn.fetch(
        """
        SELECT DISTINCT ON (platform) platform, lowest_bin
        FROM bin_history
        WHERE player_id = $1 AND lowest_bin IS NOT NULL
        ORDER BY platform, captured_at DESC
        """,
        card_id,
    )
    current_bin = {r["platform"]: r["lowest_bin"] for r in bin_rows}

    stats = await conn.fetchrow(
        """
        SELECT
            COUNT(*) FILTER (WHERE sold_at >= NOW() - INTERVAL '24 hours') AS n_24h,
            PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY sold_price)
                FILTER (WHERE sold_at >= NOW() - INTERVAL '24 hours') AS median_24h,
            STDDEV_POP(sold_price) FILTER (WHERE sold_at >= NOW() - INTERVAL '24 hours') AS stddev_24h
        FROM sales_history
        WHERE player_id = $1
        """,
        card_id,
    )
    n_24h = stats["n_24h"] or 0
    median_24h = float(stats["median_24h"]) if stats["median_24h"] is not None else None
    ps_bin = current_bin.get("ps")

    return {
        "card_id": card_id,
        "currentBin": current_bin,
        "medianSold24h": median_24h,
        "sampleSize24h": n_24h,
        "divergencePct24h": round((median_24h - ps_bin) / ps_bin * 100, 2) if (ps_bin and median_24h) else None,
        "salesPerHour24h": round(n_24h / 24.0, 2),
        "stddev24h": float(stats["stddev_24h"]) if stats["stddev_24h"] is not None else None,
    }


# --------------------------- fair value layer --------------------------------


@router.get("/fair-value/{card_id}")
async def v2_fair_value(card_id: int, request: Request, key=Depends(require_api_key)):
    row = await fv.get_card_fair_value(_player_pool(request), card_id)
    if not row:
        raise HTTPException(404, "No fair-value data for this card yet")
    return row


class FairValueBatchRequest(BaseModel):
    card_ids: List[int] = Field(..., min_length=1, max_length=100)


@router.post("/fair-value/batch")
async def v2_fair_value_batch(
    payload: FairValueBatchRequest,
    request: Request,
    key=Depends(require_api_key),
):
    """Fair value for up to 100 cards in one call - counts as a single
    request against the key's quota/rate limit rather than one per card,
    which is what makes this usable from something like a browser
    extension overlaying a whole search-results page at once."""
    card_ids = list(dict.fromkeys(payload.card_ids))  # de-dupe, keep order
    rows = await fv.get_card_fair_values_batch(_player_pool(request), card_ids)
    by_id = {str(r["card_id"]): r for r in rows}
    return {"items": by_id, "count": len(by_id)}


@router.get("/undervalued")
async def v2_undervalued(
    request: Request,
    limit: int = Query(30, ge=1, le=100),
    min_price: int = Query(1000, ge=0),
    max_price: Optional[int] = Query(None, ge=0),
    min_sales_24h: int = Query(5, ge=1),
    min_discount_pct: float = Query(3.0, ge=0),
    key=Depends(require_api_key),
):
    items = await fv.get_undervalued(
        _player_pool(request),
        limit=limit,
        min_price=min_price,
        max_price=max_price,
        min_sales_24h=min_sales_24h,
        min_discount_pct=min_discount_pct,
    )
    return {"items": items, "count": len(items)}


@router.get("/anomalies")
async def v2_anomalies(
    request: Request,
    limit: int = Query(30, ge=1, le=100),
    zscore: float = Query(-2.0, le=0),
    min_sales_24h: int = Query(8, ge=1),
    key=Depends(require_api_key),
):
    # Anomaly radar is trader/dev-tier data - the starter taster doesn't
    # include it (the free taster proves data quality; the edge is paid).
    if key.get("tier") == "starter":
        raise HTTPException(
            402,
            detail={
                "error": "tier_upgrade_required",
                "required_tier": "trader",
                "message": "Anomaly radar needs a Trader or Dev key.",
            },
        )
    items = await fv.get_anomalies(
        _player_pool(request), limit=limit, zscore_threshold=zscore, min_sales_24h=min_sales_24h
    )
    return {"items": items, "count": len(items)}


# ------------------------------ introspection --------------------------------


@router.get("/usage")
async def v2_usage(key=Depends(require_api_key), conn=Depends(get_db)):
    """The key's own quota and usage - lets consumers build backoff logic."""
    month_start = date.today().replace(day=1)
    try:
        rows = await conn.fetch(
            """
            SELECT day, requests FROM api_key_usage
            WHERE api_key_id = $1 AND day >= $2
            ORDER BY day
            """,
            key["id"], month_start,
        )
        daily = [{"day": r["day"].isoformat(), "requests": r["requests"]} for r in rows]
    except Exception:
        daily = []
    return {
        "keyPrefix": key.get("key_prefix"),
        "tier": key.get("tier"),
        "usage": key.get("usage"),
        "daily": daily,
    }
