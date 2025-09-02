# app/routers/trade_finder.py
from fastapi import APIRouter, Query, Request, HTTPException
from typing import Optional, List, Literal, Dict, Any
import asyncio
import logging

from app.services.price_history import get_price_history
from app.services.prices import get_player_price

router = APIRouter()

EA_TAX = 0.05

def _num(v, default=None, cast=float):
    try:
        if v is None or v == "": return default
        return cast(v)
    except Exception:
        return default

def _collapse_platform(p: str) -> Literal["console","pc","ps","xbox","pc"]:
    s = (p or "").lower()
    if s in ("pc", "origin"): return "pc"
    if s in ("ps","playstation","console"): return "ps"  # treat “console” as PS by default
    if s in ("xbox","xb"): return "xbox"
    return "ps"

def _csv_to_list(s: Optional[str]) -> List[str]:
    if not s: return []
    return [x.strip() for x in s.split(",") if x.strip()]

async def _chg_pct_for_hours(card_id: int, platform: str, hours: int) -> Optional[float]:
    """Compute % change over last `hours` using short history endpoint."""
    try:
        # Reuse "today" (15min buckets) and slice window here
        hist = await get_price_history(card_id, platform, "today")
        if not hist: return None
        # hist elements have keys like {"t": ms, "price": v} or {"t":..,"v":..}
        now_ms = max(int(p.get("t") or p.get("ts") or 0) for p in hist)
        cutoff = now_ms - hours * 60 * 60 * 1000
        window = [p for p in hist if int(p.get("t") or p.get("ts") or 0) >= cutoff]
        if len(window) < 2: return None
        v = lambda p: p.get("price") or p.get("v") or p.get("y")
        first = v(window[0]); last = v(window[-1])
        if not (isinstance(first, (int, float)) and isinstance(last, (int, float)) and first):
            return None
        return round(((last - first) / first) * 100.0, 2)
    except Exception:
        return None

async def _vol_score(card_id: int, platform: str, hours: int) -> Optional[float]:
    """Very crude liquidity proxy: normalized count of ticks in window (0..1)."""
    try:
        hist = await get_price_history(card_id, platform, "today")
        if not hist: return None
        now_ms = max(int(p.get("t") or p.get("ts") or 0) for p in hist)
        cutoff = now_ms - hours * 60 * 60 * 1000
        window = [p for p in hist if int(p.get("t") or p.get("ts") or 0) >= cutoff]
        # assume ~4 ticks/hour typical; cap at 1.0
        return round(min(1.0, (len(window) / max(1, hours * 4))), 3)
    except Exception:
        return None

@router.get("/trade-finder")
async def trade_finder(
    request: Request,
    platform: str = Query("console", pattern="^(console|pc|ps|xbox)$"),
    timeframe: int = Query(24, ge=4, le=24),  # 4h or 24h
    topn: int = Query(20, ge=1, le=50),

    budget_min: Optional[float] = Query(None),
    budget_max: Optional[float] = Query(None),
    min_profit: Optional[float] = Query(None),
    min_margin_pct: Optional[float] = Query(None),

    rating_min: Optional[int] = Query(None),
    rating_max: Optional[int] = Query(None),

    leagues: Optional[str] = Query(None, description="Comma separated"),
    nations: Optional[str] = Query(None, description="Comma separated"),
    positions: Optional[str] = Query(None, description="Comma separated"),

    exclude_extinct: int = Query(1),
    exclude_low_liquidity: int = Query(0),
    exclude_anomalies: int = Query(1),

    debug: int = Query(0),
):
    player_pool = getattr(request.app.state, "player_pool", None)
    if player_pool is None:
        raise HTTPException(500, "Player DB pool not initialised")

    plat = _collapse_platform(platform)
    hours = 4 if int(timeframe) <= 6 else 24

    # sanitize numeric filters
    budget_min = _num(budget_min, None, float)
    budget_max = _num(budget_max, None, float)
    min_profit = _num(min_profit, None, float)
    min_margin_pct = _num(min_margin_pct, None, float)
    rating_min = _num(rating_min, None, int)
    rating_max = _num(rating_max, None, int)

    # text filters
    leagues_list = _csv_to_list(leagues)
    nations_list = _csv_to_list(nations)
    positions_list = _csv_to_list(positions)

    try:
        # Build catalog WHERE on metadata only (prices come live)
        where: List[str] = []
        params: List[Any] = []

        if rating_min is not None:
            params.append(rating_min); where.append(f"rating >= ${len(params)}")
        if rating_max is not None:
            params.append(rating_max); where.append(f"rating <= ${len(params)}")

        if leagues_list:
            params.append(leagues_list)
            where.append(f"LOWER(league) = ANY(SELECT LOWER(x) FROM unnest(${len(params)}::text[] ) AS t(x))")
        if nations_list:
            params.append(nations_list)
            where.append(f"LOWER(nation) = ANY(SELECT LOWER(x) FROM unnest(${len(params)}::text[] ) AS t(x))")
        if positions_list:
            # match position or any altposition token
            params.append([p.upper() for p in positions_list])
            idx = len(params)
            where.append(
                f"""(
                    UPPER(position) = ANY(${idx}::text[])
                    OR (
                        COALESCE(altposition, '') <> ''
                        AND EXISTS (
                          SELECT 1
                          FROM regexp_split_to_table(altposition, '[,;/|\\s]+') ap
                          WHERE UPPER(TRIM(ap)) = ANY(${idx}::text[])
                        )
                    )
                )"""
            )

        # Reasonable candidate cap to keep price lookups snappy
        sql = f"""
          SELECT card_id::text AS cid, name, version, rating, image_url, club, league, position
          FROM fut_players
          {"WHERE " + " AND ".join(where) if where else ""}
          ORDER BY rating DESC NULLS LAST, name ASC
          LIMIT 250
        """

        async with player_pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)

        # Pull live prices concurrently
        async def enrich(r):
            cid = int(r["cid"])
            price_now = await get_player_price(cid, plat)
            if not isinstance(price_now, (int, float)) or price_now <= 0:
                return None

            # Budget filters apply to current price
            if budget_min is not None and price_now < budget_min:
                return None
            if budget_max is not None and price_now > budget_max:
                return None

            # Use requested min_margin_pct (default 8%)
            target_margin = (min_margin_pct if min_margin_pct is not None else 8.0) / 100.0
            expected_sell = int(round(price_now * (1 + target_margin)))

            net_profit = int(round(expected_sell * (1 - EA_TAX) - price_now))
            margin_pct = round(((expected_sell - price_now) / price_now) * 100.0, 2)

            # Enforce min_profit / min_margin filters
            if min_profit is not None and net_profit < min_profit:
                return None
            if min_margin_pct is not None and margin_pct < min_margin_pct:
                return None

            # Optional extras
            chg = await _chg_pct_for_hours(cid, plat, hours)
            vol = await _vol_score(cid, plat, hours)

            # Extinct or anomalies – you can wire proper signals later
            if exclude_extinct and price_now <= 0:
                return None
            if exclude_low_liquidity and (vol is None or vol < 0.05):
                return None
            # exclude_anomalies placeholder: no-op for now

            return {
                "player_id": cid,
                "card_id": cid,
                "name": r["name"],
                "version": r.get("version"),
                "rating": r.get("rating"),
                "position": r.get("position"),
                "league": r.get("league"),
                "image_url": r.get("image_url"),

                "platform": "console" if plat in ("ps","xbox") else "pc",
                "timeframe_hours": hours,

                "current_price": int(price_now),
                "expected_sell": expected_sell,
                "est_profit_after_tax": net_profit,
                "margin_pct": margin_pct,

                "change_pct_window": chg,
                "vol_score": vol,

                # keep these so your UI chips are stable
                "tags": [],
                "seasonal_shift": None,
            }

        results = await asyncio.gather(*(enrich(r) for r in rows), return_exceptions=True)
        items = []
        for it in results:
            if it is None: 
                continue
            if isinstance(it, Exception):
                logging.debug("deal enrich error: %s", it)
                continue
            items.append(it)

        # crude ranking: higher net profit, then margin, then rating
        items.sort(key=lambda d: (d["est_profit_after_tax"], d["margin_pct"], d.get("rating") or 0), reverse=True)
        items = items[:topn]

        return {
            "items": items,
            "meta": {
                "returned": len(items),
                "platform_price_source": plat,
            },
        }

    except Exception as e:
        logging.exception("trade_finder error")
        if debug:
            raise HTTPException(500, detail=str(e))
        raise HTTPException(500, "Internal error")
