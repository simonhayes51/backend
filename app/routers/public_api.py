# app/routers/public_api.py
"""
Read-only historical-data API for external consumers (bots, spreadsheet
tools, third-party dashboards) - authenticated by API key
(app/auth/api_keys.py), not the session cookie the rest of the app uses.

This is the thing FUTBIN/FUT.GG don't offer: structured, paid access to
real historical BIN + completed-sales data instead of having to scrape
HTML. Mirrors app/routers/players.py's data endpoints but under a
versioned, API-key-gated prefix so the consumer-facing routes above can
keep evolving independently of this contract.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query

from app.auth.api_keys import require_api_key
from app.db import get_player_db

router = APIRouter(prefix="/api/public/v1", tags=["public-api"])


@router.get("/players/search")
async def public_search_players(
    q: str = Query("", description="name or card_id substring"),
    limit: int = Query(50, le=200),
    key = Depends(require_api_key),
    conn = Depends(get_player_db),
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


@router.get("/players/{card_id}/bin-history")
async def public_bin_history(
    card_id: int,
    platform: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=2000),
    key = Depends(require_api_key),
    conn = Depends(get_player_db),
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
async def public_sales_history(
    card_id: int,
    limit: int = Query(50, ge=1, le=500),
    key = Depends(require_api_key),
    conn = Depends(get_player_db),
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


@router.get("/players/{card_id}/market-metrics")
async def public_market_metrics(
    card_id: int,
    key = Depends(require_api_key),
    conn = Depends(get_player_db),
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
