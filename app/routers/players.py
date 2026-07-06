# app/routers/players.py

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

# If you already have this service, it's safe to import (it doesn't import main)
from app.services.price_history import get_price_history
from app.db import get_player_db
from app.futbin_client import fetch_price_by_url

router = APIRouter(prefix="/api/players", tags=["players"])

# ------------------------------
# Helpers
# ------------------------------

def _plat(p: str) -> str:
    p = (p or "").lower()
    if p in ("ps", "playstation", "console"):
        return "ps"
    if p in ("xbox", "xb"):
        return "xbox"
    if p in ("pc", "origin"):
        return "pc"
    return "ps"

# ------------------------------
# Endpoints
# ------------------------------

@router.get("/resolve")
async def resolve_player_by_name(
    name: str = Query(..., description="Player name to resolve"),
    conn = Depends(get_player_db),
):
    """
    Resolve a player by name to get their card details
    """
    try:
        # Search for exact match first, then fuzzy match
        row = await conn.fetchrow(
            """
            SELECT card_id, name, rating, version, image_url, club, league, nation, position, altposition, price_num, price
            FROM fut_players 
            WHERE LOWER(name) = LOWER($1)
            ORDER BY rating DESC
            LIMIT 1
            """,
            name.strip()
        )
        
        if not row:
            # Try fuzzy matching
            row = await conn.fetchrow(
                """
                SELECT card_id, name, rating, version, image_url, club, league, nation, position, altposition, price_num, price
                FROM fut_players 
                WHERE LOWER(name) ILIKE LOWER($1)
                ORDER BY rating DESC
                LIMIT 1
                """,
                f"%{name.strip()}%"
            )
        
        if not row:
            raise HTTPException(status_code=404, detail="Player not found")
        
        return {
            "card_id": int(row["card_id"]),
            "name": row["name"],
            "rating": row["rating"],
            "version": row["version"],
            "image_url": row["image_url"],
            "club": row["club"],
            "league": row["league"],
            "nation": row["nation"],
            "position": row["position"],
            "altposition": row["altposition"],
            "price": row["price"],
            "price_num": row["price_num"],
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Resolution failed: {e}")

@router.get("/search")
async def search_players(
    q: str = Query("", description="name or card_id substring"),
    pos: Optional[str] = Query(None, description="exact position code like ST, CAM, CB"),
    limit: int = Query(50, description="max results"),
    conn = Depends(get_player_db),
):
    q = (q or "").strip()
    p = (pos or "").strip().upper() or None

    where: List[str] = []
    params: List[Any] = []

    if q:
        where.append("(LOWER(name) LIKE LOWER($1) OR card_id::text LIKE $1)")
        params.append(f"%{q}%")

    if p:
        params.append(p)
        idx = len(params)  # position param index
        where.append(
            f"""
            (
              UPPER(position) = ${idx}
              OR (
                COALESCE(altposition, '') <> ''
                AND EXISTS (
                  SELECT 1
                  FROM regexp_split_to_table(altposition, '[,;/|\\s]+') ap
                  WHERE UPPER(TRIM(ap)) = ${idx}
                )
              )
            )
            """
        )

    base_where = " AND ".join(where) if where else "TRUE"
    limit_idx = len(params) + 1

    sql = f"""
        SELECT
          card_id, name, rating, version, image_url, club, league, nation,
          position, altposition, price, price_num
        FROM fut_players
        WHERE {base_where}
        ORDER BY
          CASE WHEN price IS NULL THEN 1 ELSE 0 END,
          rating DESC NULLS LAST,
          name ASC
        LIMIT ${limit_idx}
    """

    params.append(limit)
    rows = await conn.fetch(sql, *params)

    players = [
        {
            "card_id": int(r["card_id"]),
            "name": r["name"],
            "rating": r["rating"],
            "version": r["version"],
            "image_url": r["image_url"],
            "club": r["club"],
            "league": r["league"],
            "nation": r["nation"],
            "position": r["position"],
            "altposition": r["altposition"],
            "price": r["price"],
            "price_num": r["price_num"],
        }
        for r in rows
    ]

    return {"players": players, "data": players}

@router.get("/autocomplete")
async def players_autocomplete(
    q: str = Query("", description="name or card_id substring"),
    pos: Optional[str] = Query(None, description="position filter like ST, CAM, CB"),
    conn = Depends(get_player_db),
):
    """
    Lightweight autocomplete list for UI dropdowns.
    Returns items with {value, label, card_id, name, rating, version, image_url, position}.
    """
    q = (q or "").strip()
    p = (pos or "").strip().upper() or None

    where: List[str] = []
    params: List[Any] = []

    if q:
        where.append("(LOWER(name) LIKE LOWER($1) OR card_id::text LIKE $1)")
        params.append(f"%{q}%")

    if p:
        params.append(p)
        idx = len(params)
        where.append(
            f"""
            (
              UPPER(position) = ${idx}
              OR (
                COALESCE(altposition, '') <> ''
                AND EXISTS (
                  SELECT 1
                  FROM regexp_split_to_table(altposition, '[,;/|\\s]+') ap
                  WHERE UPPER(TRIM(ap)) = ${idx}
                )
              )
            )
            """
        )

    base_where = " AND ".join(where) if where else "TRUE"
    sql = f"""
        SELECT card_id, name, rating, version, image_url, position
        FROM fut_players
        WHERE {base_where}
        ORDER BY rating DESC NULLS LAST, name ASC
        LIMIT 20
    """
    rows = await conn.fetch(sql, *params)

    items: List[Dict[str, Any]] = []
    for r in rows:
        cid = int(r["card_id"])
        name = r["name"]
        rating = r["rating"]
        ver = r["version"] or ""
        pos_label = r["position"] or ""
        label = f"{name} ({rating}) {ver} {pos_label}".strip()
        items.append({
            "value": cid,
            "label": label,
            "card_id": cid,
            "name": name,
            "rating": rating,
            "version": ver,
            "image_url": r["image_url"],
            "position": pos_label,
        })

    return {"items": items}


# NOTE: /batch/... routes must be registered before /{card_id}/... routes.
# card_id is typed str on some routes and int on others, and Starlette
# matches path segments before FastAPI's parameter validation runs - so a
# request to /batch/meta would otherwise be captured by /{card_id}/meta
# (card_id="batch", str always validates) instead of ever reaching this
# handler. Confirmed via isolated test: this silently broke /batch/meta
# for as long as it was declared after /{card_id}/meta.
@router.get("/batch/meta")
async def batch_meta(
    ids: str = Query(..., description="CSV of card_ids"),
    conn = Depends(get_player_db),
):
    """
    Batch metadata fetch for up to ~100 ids at once.
    """
    raw_ids = [x.strip() for x in ids.split(",") if x.strip()]
    if not raw_ids:
        return {"items": []}
    rows = await conn.fetch(
        """
        SELECT card_id, name, rating, version, image_url, club, league, nation, position, altposition, price, price_num
        FROM fut_players
        WHERE card_id::text = ANY($1::text[])
        """,
        raw_ids,
    )
    out: List[Dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        d["card_id"] = int(d["card_id"])
        out.append(d)
    return {"items": out}


@router.get("/batch/market-metrics")
async def batch_market_metrics(
    ids: str = Query(..., description="CSV of card_ids, up to ~150 at once"),
    conn = Depends(get_player_db),
):
    """
    Batch version of /{card_id}/market-metrics for list views (search
    results, watchlist, trade finder) that need liquidity/real-price/snipe
    signals for many cards without one request per card.
    """
    raw_ids = [x.strip() for x in ids.split(",") if x.strip()][:150]
    if not raw_ids:
        return {"items": {}}
    try:
        id_list = [int(x) for x in raw_ids]
    except ValueError:
        raise HTTPException(status_code=400, detail="ids must be integers")

    try:
        stats_rows = await conn.fetch(
            """
            WITH stats AS (
                SELECT
                    player_id,
                    COUNT(*) FILTER (WHERE sold_at >= NOW() - INTERVAL '1 hour') AS n_1h,
                    COUNT(*) FILTER (WHERE sold_at >= NOW() - INTERVAL '24 hours') AS n_24h,
                    COUNT(*) FILTER (WHERE sold_at >= NOW() - INTERVAL '7 days') AS n_7d,
                    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY sold_price)
                        FILTER (WHERE sold_at >= NOW() - INTERVAL '24 hours') AS median_24h,
                    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY sold_price)
                        FILTER (WHERE sold_at >= NOW() - INTERVAL '7 days') AS median_7d,
                    STDDEV_POP(sold_price) FILTER (WHERE sold_at >= NOW() - INTERVAL '24 hours') AS stddev_24h,
                    STDDEV_POP(sold_price) FILTER (WHERE sold_at >= NOW() - INTERVAL '7 days') AS stddev_7d
                FROM sales_history
                WHERE player_id = ANY($1::bigint[])
                GROUP BY player_id
            ),
            snipe AS (
                SELECT sh.player_id, COUNT(*) AS snipe_count_24h
                FROM sales_history sh
                JOIN stats s ON s.player_id = sh.player_id
                WHERE sh.sold_at >= NOW() - INTERVAL '24 hours'
                  AND s.median_24h IS NOT NULL
                  AND sh.sold_price <= s.median_24h * 0.85
                GROUP BY sh.player_id
            )
            SELECT
                stats.player_id, stats.n_1h, stats.n_24h, stats.n_7d,
                stats.median_24h, stats.median_7d, stats.stddev_24h, stats.stddev_7d,
                COALESCE(snipe.snipe_count_24h, 0) AS snipe_count_24h
            FROM stats
            LEFT JOIN snipe ON snipe.player_id = stats.player_id
            """,
            id_list,
        )

        bin_rows = await conn.fetch(
            """
            SELECT DISTINCT ON (player_id, platform) player_id, platform, lowest_bin
            FROM bin_history
            WHERE player_id = ANY($1::bigint[]) AND lowest_bin IS NOT NULL
            ORDER BY player_id, platform, captured_at DESC
            """,
            id_list,
        )

        bins_by_card: Dict[int, Dict[str, int]] = {}
        for r in bin_rows:
            bins_by_card.setdefault(int(r["player_id"]), {})[r["platform"]] = r["lowest_bin"]

        items: Dict[str, Any] = {}
        for r in stats_rows:
            cid = int(r["player_id"])
            n_24h = r["n_24h"] or 0
            median_24h = float(r["median_24h"]) if r["median_24h"] is not None else None
            stddev_24h = float(r["stddev_24h"]) if r["stddev_24h"] is not None else None
            ps_bin = bins_by_card.get(cid, {}).get("ps")

            divergence_pct_24h = None
            if ps_bin and median_24h is not None:
                divergence_pct_24h = round((median_24h - ps_bin) / ps_bin * 100, 2)

            cv_24h = round((stddev_24h or 0) / median_24h, 4) if median_24h else None
            snipe_index_24h = round((r["snipe_count_24h"] or 0) / n_24h, 3) if n_24h > 0 else None

            margin = None
            if ps_bin and median_24h is not None:
                net_after_tax = median_24h * 0.95
                est_margin_coins = round(net_after_tax - ps_bin)
                margin = {
                    "buyAt": ps_bin,
                    "sellAt": round(median_24h),
                    "netAfterTax": round(net_after_tax),
                    "estMarginCoins": est_margin_coins,
                    "estMarginPct": round(est_margin_coins / ps_bin * 100, 2),
                }

            items[str(cid)] = {
                "currentBin": bins_by_card.get(cid, {}),
                "realPrice": {
                    "medianSold24h": median_24h,
                    "medianSold7d": float(r["median_7d"]) if r["median_7d"] is not None else None,
                    "sampleSize24h": n_24h,
                    "sampleSize7d": r["n_7d"] or 0,
                },
                "divergencePct24h": divergence_pct_24h,
                "liquidity": {
                    "salesLast1h": r["n_1h"] or 0,
                    "salesLast24h": n_24h,
                    "salesPerHour24h": round(n_24h / 24.0, 2),
                },
                "volatility": {
                    "stddev24h": stddev_24h,
                    "stddev7d": float(r["stddev_7d"]) if r["stddev_7d"] is not None else None,
                    "coefficientOfVariation24h": cv_24h,
                },
                "snipeIndex24h": snipe_index_24h,
                "taxAwareMargin": margin,
            }

        return {"items": items}
    except Exception:
        return {"items": {}}


@router.get("/{card_id}")
async def get_player(card_id: str, conn = Depends(get_player_db)):
    """
    Return a single player's metadata from fut_players by card_id.
    """
    row = await conn.fetchrow(
        """
        SELECT card_id, name, rating, version, image_url, club, league, nation, position, altposition, price, price_num
        FROM fut_players
        WHERE card_id::text = $1
        """,
        str(card_id),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Player not found")
    d = dict(row)
    d["card_id"] = int(d["card_id"])
    return d

@router.get("/{card_id}/meta")
async def get_player_meta(card_id: str, conn = Depends(get_player_db)):
    """
    Small alias for metadata (same as GET /{card_id}) to keep clients flexible.
    """
    return await get_player(card_id, conn)  # type: ignore[arg-type]

@router.get("/{card_id}/price")
async def get_player_price_route(
    card_id: int,
    platform: str = Query("ps", description="ps|xbox|pc|console"),
    conn = Depends(get_player_db),
):
    """
    Return latest price preferring local data:
      1) fut_candles last close (per platform)
      2) fut_players.price_num snapshot
      3) FUT.GG currentPrice (fallback)
    """
    plat = _plat(platform)

    # 1) Try fut_candles (latest close) with correct per-platform filter
    try:
        where: List[str] = ["player_card_id::text = $1"]
        params: List[Any] = [str(card_id)]

        if plat == "ps":
            where.append("platform IN ('ps','playstation','console')")
        else:
            params.append(plat)
            where.append(f"platform = ${len(params)}")

        sql = f"""
            SELECT close, open_time
            FROM fut_candles
            WHERE {' AND '.join(where)}
            ORDER BY open_time DESC
            LIMIT 1
        """
        row = await conn.fetchrow(sql, *params)
        if row and row["close"] is not None:
            return {
                "card_id": card_id,
                "platform": plat,
                "price": int(row["close"]),
                "isExtinct": False,
                "updatedAt": row["open_time"],
                "source": "candles",
            }
    except Exception:
        pass

    # 2) fut_players snapshot
    try:
        row = await conn.fetchrow(
            """
            SELECT price_num, updated_at
            FROM fut_players
            WHERE card_id::text = $1
            """,
            str(card_id),
        )
        if row and row["price_num"] is not None:
            return {
                "card_id": card_id,
                "platform": plat,
                "price": int(row["price_num"]),
                "isExtinct": False,
                "updatedAt": row["updated_at"],
                "source": "players",
            }
    except Exception:
        pass

    # 3) futbin.com live fetch (on-demand, freshest available - seconds old).
    try:
        player_url = await conn.fetchval(
            "SELECT player_url FROM fut_players WHERE card_id::text = $1",
            str(card_id),
        )
        if player_url:
            price = await fetch_price_by_url(player_url, plat)
            if price is not None:
                return {
                    "card_id": card_id,
                    "platform": plat,
                    "price": price,
                    "isExtinct": False,
                    "updatedAt": None,
                    "source": "futbin",
                }
    except Exception:
        pass

    return {
        "card_id": card_id,
        "platform": plat,
        "price": None,
        "isExtinct": False,
        "updatedAt": None,
        "source": "none",
    }


@router.get("/{card_id}/history")
async def get_player_history_route(
    card_id: int,
    platform: str = Query("ps", description="ps|xbox|pc|console"),
    tf: str = Query("today", description="today|24h|7d|30d|all"),
    conn = Depends(get_player_db),
):
    """
    Return OHLC candles for a player.
    Order: futbin-backed service first; if empty, fall back to fut_candles.
    Includes 'ts' (epoch seconds) + 'iso' for robust client parsing.
    """
    plat = _plat(platform)

    # 1) Try futbin-backed service (get_price_history returns {"points": [...]})
    try:
        data = await get_price_history(card_id, plat, tf)
        points = data.get("points") if isinstance(data, dict) else None
        if points:
            candles = []
            for pt in points:
                price = pt.get("price")
                iso = pt.get("t")
                if price is None or not iso:
                    continue
                try:
                    ts = int(datetime.fromisoformat(iso).timestamp())
                except Exception:
                    continue
                candles.append({
                    "ts": ts,
                    "iso": iso,
                    "open_time": iso,  # compat
                    "open": int(price),
                    "high": int(price),
                    "low": int(price),
                    "close": int(price),
                })
            if candles:
                return {
                    "card_id": card_id,
                    "platform": plat,
                    "tf": tf,
                    "history": candles,
                    "source": "futbin",
                }
    except Exception:
        pass  # fall through

    # 2) DB fallback
    try:
        tf_map = {
            "today": "2 days",
            "24h":  "1 day",
            "7d":   "7 days",
            "30d":  "30 days",
            "all":  None,
        }
        window = tf_map.get(tf, "2 days")

        where: List[str] = ["player_card_id::text = $1"]
        params: List[Any] = [str(card_id)]

        if plat == "ps":
            where.append("platform IN ('ps','playstation','console')")
        else:
            params.append(plat)
            where.append(f"platform = ${len(params)}")

        if window:
            params.append(window)  # e.g. "24 hours"
            where.append(f"open_time >= now() - (${len(params)}::interval)")

        sql = f"""
            SELECT open_time, open, high, low, close
            FROM fut_candles
            WHERE {' AND '.join(where)}
            ORDER BY open_time ASC
            LIMIT 2000
        """
        rows = await conn.fetch(sql, *params)

        candles = [
            {
                "ts":  int(r["open_time"].timestamp()),
                "iso": r["open_time"].isoformat(),
                "open_time": r["open_time"].isoformat(),  # compat
                "open":   int(r["open"])   if r["open"]   is not None else None,
                "high":   int(r["high"])   if r["high"]   is not None else None,
                "low":    int(r["low"])    if r["low"]    is not None else None,
                "close":  int(r["close"])  if r["close"]  is not None else None,
            }
            for r in rows
        ]

        if not candles:
            where2: List[str] = ["player_card_id::text = $1"]
            params2: List[Any] = [str(card_id)]
            if plat == "ps":
                where2.append("platform IN ('ps','playstation','console')")
            else:
                params2.append(plat)
                where2.append(f"platform = ${len(params2)}")

            sql2 = f"""
                SELECT open_time, open, high, low, close
                FROM fut_candles
                WHERE {' AND '.join(where2)}
                ORDER BY open_time ASC
                LIMIT 2000
            """
            rows = await conn.fetch(sql2, *params2)
            candles = [
                {
                    "ts":  int(r["open_time"].timestamp()),
                    "iso": r["open_time"].isoformat(),
                    "open_time": r["open_time"].isoformat(),
                    "open": int(r["open"]) if r["open"] is not None else None,
                    "high": int(r["high"]) if r["high"] is not None else None,
                    "low":  int(r["low"])  if r["low"]  is not None else None,
                    "close":int(r["close"])if r["close"]is not None else None,
                }
                for r in rows
            ]

        return {
            "card_id": card_id,
            "platform": plat,
            "tf": tf,
            "history": candles,
            "source": "candles",
        }
    except Exception:
        return {"card_id": card_id, "platform": plat, "tf": tf, "history": [], "source": "none"}


@router.get("/{card_id}/bin-history")
async def get_player_bin_history_route(
    card_id: int,
    platform: Optional[str] = Query(None, description="ps|xbox|pc|console - omit for both markets"),
    limit: int = Query(200, ge=1, le=2000),
    conn = Depends(get_player_db),
):
    """
    Timeline of lowest-BIN snapshots from bin_history (populated by the
    separate auto_sync bin_sales_history_sync.py worker every 10 minutes -
    this endpoint only reads it, it never writes). Each row is one point in
    time; nothing is ever overwritten on the collector side, so this can
    return the same price repeated across many captured_at timestamps if
    the market hasn't moved.
    """
    plat = _plat(platform) if platform else None
    try:
        where: List[str] = ["player_id = $1"]
        params: List[Any] = [card_id]
        if plat:
            params.append(plat)
            where.append(f"platform = ${len(params)}")
        params.append(limit)

        sql = f"""
            SELECT platform, lowest_bin, captured_at
            FROM bin_history
            WHERE {' AND '.join(where)}
            ORDER BY captured_at DESC
            LIMIT ${len(params)}
        """
        rows = await conn.fetch(sql, *params)
        points = [
            {
                "platform": r["platform"],
                "lowestBin": r["lowest_bin"],
                "capturedAt": r["captured_at"].isoformat(),
            }
            for r in rows
        ]
        return {"card_id": card_id, "platform": plat, "points": points}
    except Exception:
        # bin_history/sales_history live in the same DB as fut_players but
        # are populated by a separate service - if that service's tables
        # somehow aren't there yet (fresh DB, not deployed), fail soft
        # rather than 500 a page that's otherwise working fine.
        return {"card_id": card_id, "platform": plat, "points": []}


@router.get("/{card_id}/sales-history")
async def get_player_sales_history_route(
    card_id: int,
    limit: int = Query(50, ge=1, le=500),
    conn = Depends(get_player_db),
):
    """
    Real completed sales from sales_history (populated by the same
    bin_sales_history_sync.py worker, deduped on (player_id, sold_at,
    sold_price) at write time - read-only here). PS/Xbox market only, per
    the collector's own scope. Most recent first.
    """
    try:
        rows = await conn.fetch(
            """
            SELECT listed_price, sold_price, ea_tax, net_price, sold_at, captured_at
            FROM sales_history
            WHERE player_id = $1
            ORDER BY sold_at DESC
            LIMIT $2
            """,
            card_id, limit,
        )
        sales = [
            {
                "listedPrice": r["listed_price"],
                "soldPrice": r["sold_price"],
                "eaTax": r["ea_tax"],
                "netPrice": r["net_price"],
                "soldAt": r["sold_at"].isoformat(),
                "capturedAt": r["captured_at"].isoformat(),
            }
            for r in rows
        ]
        return {"card_id": card_id, "platform": "ps", "sales": sales}
    except Exception:
        return {"card_id": card_id, "platform": "ps", "sales": []}


@router.get("/{card_id}/sales-candles")
async def get_player_sales_candles_route(
    card_id: int,
    bucket_hours: float = Query(1, gt=0, le=24, description="candle width in hours"),
    days: int = Query(7, ge=1, le=30, description="lookback window in days"),
    conn = Depends(get_player_db),
):
    """
    Real OHLC candles built from actual completed sales (sales_history),
    not the BIN-snapshot line the frontend used to show. Also returns the
    raw sale points in the same window so the chart can overlay individual
    sale markers on top of the candles. PS market only, per the collector's
    scope.
    """
    try:
        bucket_seconds = bucket_hours * 3600
        rows = await conn.fetch(
            """
            WITH bucketed AS (
                SELECT
                    to_timestamp(floor(extract(epoch FROM sold_at) / $2::float) * $2::float) AS bucket_start,
                    sold_price,
                    sold_at,
                    ROW_NUMBER() OVER (
                        PARTITION BY floor(extract(epoch FROM sold_at) / $2::float)
                        ORDER BY sold_at ASC
                    ) AS rn_open,
                    ROW_NUMBER() OVER (
                        PARTITION BY floor(extract(epoch FROM sold_at) / $2::float)
                        ORDER BY sold_at DESC
                    ) AS rn_close
                FROM sales_history
                WHERE player_id = $1 AND sold_at >= NOW() - ($3 || ' days')::interval
            )
            SELECT
                bucket_start,
                MAX(CASE WHEN rn_open = 1 THEN sold_price END) AS open,
                MAX(sold_price) AS high,
                MIN(sold_price) AS low,
                MAX(CASE WHEN rn_close = 1 THEN sold_price END) AS close,
                COUNT(*) AS volume
            FROM bucketed
            GROUP BY bucket_start
            ORDER BY bucket_start ASC
            """,
            card_id, bucket_seconds, days,
        )
        candles = [
            {
                "time": int(r["bucket_start"].timestamp()),
                "open": r["open"],
                "high": r["high"],
                "low": r["low"],
                "close": r["close"],
                "volume": r["volume"],
            }
            for r in rows
        ]

        # Capped well below what a high-liquidity card can produce (seen up
        # to ~12k sales/day in production) - beyond this, individual markers
        # would just paint a solid line on top of the candles instead of
        # conveying anything; the candle bodies/wicks already are the real
        # sale data at that volume, just aggregated.
        sale_rows = await conn.fetch(
            """
            SELECT sold_price, sold_at
            FROM sales_history
            WHERE player_id = $1 AND sold_at >= NOW() - ($2 || ' days')::interval
            ORDER BY sold_at ASC
            LIMIT 300
            """,
            card_id, days,
        )
        sales = [
            {"time": int(r["sold_at"].timestamp()), "price": r["sold_price"]}
            for r in sale_rows
        ]

        return {"card_id": card_id, "platform": "ps", "bucketHours": bucket_hours, "candles": candles, "sales": sales}
    except Exception:
        return {"card_id": card_id, "platform": "ps", "bucketHours": bucket_hours, "candles": [], "sales": []}


@router.get("/{card_id}/market-metrics")
async def get_player_market_metrics_route(
    card_id: int,
    conn = Depends(get_player_db),
):
    """
    The real numbers a trader needs that plain BIN price doesn't give you,
    computed from bin_history/sales_history: real sold price vs current
    BIN, liquidity (sales/hour), volatility (sold-price stddev), a snipe
    index (how often sales clear well below the trailing median), and a
    tax-aware margin estimate. sales_history only covers the PS market, so
    BIN comparisons here use the ps platform to match.
    """
    try:
        bin_rows = await conn.fetch(
            """
            SELECT DISTINCT ON (platform) platform, lowest_bin, captured_at
            FROM bin_history
            WHERE player_id = $1 AND lowest_bin IS NOT NULL
            ORDER BY platform, captured_at DESC
            """,
            card_id,
        )
        current_bin = {r["platform"]: r["lowest_bin"] for r in bin_rows}
        ps_bin = current_bin.get("ps")

        stats = await conn.fetchrow(
            """
            WITH stats AS (
                SELECT
                    COUNT(*) FILTER (WHERE sold_at >= NOW() - INTERVAL '1 hour') AS n_1h,
                    COUNT(*) FILTER (WHERE sold_at >= NOW() - INTERVAL '24 hours') AS n_24h,
                    COUNT(*) FILTER (WHERE sold_at >= NOW() - INTERVAL '7 days') AS n_7d,
                    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY sold_price)
                        FILTER (WHERE sold_at >= NOW() - INTERVAL '24 hours') AS median_24h,
                    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY sold_price)
                        FILTER (WHERE sold_at >= NOW() - INTERVAL '7 days') AS median_7d,
                    STDDEV_POP(sold_price) FILTER (WHERE sold_at >= NOW() - INTERVAL '24 hours') AS stddev_24h,
                    STDDEV_POP(sold_price) FILTER (WHERE sold_at >= NOW() - INTERVAL '7 days') AS stddev_7d
                FROM sales_history
                WHERE player_id = $1
            )
            SELECT
                stats.*,
                (
                    SELECT COUNT(*) FROM sales_history
                    WHERE player_id = $1
                      AND sold_at >= NOW() - INTERVAL '24 hours'
                      AND sold_price <= stats.median_24h * 0.85
                ) AS snipe_count_24h
            FROM stats
            """,
            card_id,
        )

        n_1h = stats["n_1h"] or 0
        n_24h = stats["n_24h"] or 0
        n_7d = stats["n_7d"] or 0
        median_24h = float(stats["median_24h"]) if stats["median_24h"] is not None else None
        median_7d = float(stats["median_7d"]) if stats["median_7d"] is not None else None
        stddev_24h = float(stats["stddev_24h"]) if stats["stddev_24h"] is not None else None
        stddev_7d = float(stats["stddev_7d"]) if stats["stddev_7d"] is not None else None
        snipe_count_24h = stats["snipe_count_24h"] or 0

        divergence_pct_24h = None
        if ps_bin and median_24h is not None:
            divergence_pct_24h = round((median_24h - ps_bin) / ps_bin * 100, 2)

        cv_24h = round((stddev_24h or 0) / median_24h, 4) if median_24h else None

        snipe_index_24h = round(snipe_count_24h / n_24h, 3) if n_24h > 0 else None

        margin = None
        if ps_bin and median_24h is not None:
            net_after_tax = median_24h * 0.95
            est_margin_coins = round(net_after_tax - ps_bin)
            margin = {
                "buyAt": ps_bin,
                "sellAt": round(median_24h),
                "netAfterTax": round(net_after_tax),
                "estMarginCoins": est_margin_coins,
                "estMarginPct": round(est_margin_coins / ps_bin * 100, 2),
            }

        return {
            "card_id": card_id,
            "currentBin": current_bin,
            "realPrice": {
                "medianSold24h": median_24h,
                "medianSold7d": median_7d,
                "sampleSize24h": n_24h,
                "sampleSize7d": n_7d,
            },
            "divergencePct24h": divergence_pct_24h,
            "liquidity": {
                "salesLast1h": n_1h,
                "salesLast24h": n_24h,
                "salesPerHour24h": round(n_24h / 24.0, 2),
            },
            "volatility": {
                "stddev24h": stddev_24h,
                "stddev7d": stddev_7d,
                "coefficientOfVariation24h": cv_24h,
            },
            "snipeIndex24h": snipe_index_24h,
            "taxAwareMargin": margin,
        }
    except Exception:
        return {
            "card_id": card_id,
            "currentBin": {},
            "realPrice": {"medianSold24h": None, "medianSold7d": None, "sampleSize24h": 0, "sampleSize7d": 0},
            "divergencePct24h": None,
            "liquidity": {"salesLast1h": 0, "salesLast24h": 0, "salesPerHour24h": 0},
            "volatility": {"stddev24h": None, "stddev7d": None, "coefficientOfVariation24h": None},
            "snipeIndex24h": None,
            "taxAwareMargin": None,
        }
