# app/routers/trade_finder.py
from fastapi import APIRouter, Query, Request, HTTPException
from typing import Optional, List, Literal, Dict, Any
import asyncio
import logging

from app.services.prices import get_player_price  # live price per platform

router = APIRouter(prefix="/api")

# --- helpers --------------------------------------------------------------

def _collapse_platform(p: str) -> Literal["console", "pc"]:
    s = (p or "").lower()
    return "pc" if s in ("pc", "origin") else "console"

def _price_platform(platform_collapse: Literal["console", "pc"]) -> Literal["ps", "pc"]:
    # For console we default to PS pricing (most liquid); you can switch to 'xbox' if desired.
    return "pc" if platform_collapse == "pc" else "ps"

def _num(v, default=None, cast=float):
    try:
        if v is None or v == "": 
            return default
        return cast(v)
    except Exception:
        return default

# --- API ------------------------------------------------------------------

@router.get("/trade-finder")
async def trade_finder(
    request: Request,
    platform: str = Query("console", pattern="^(console|pc)$"),
    timeframe: int = Query(24, ge=6, le=24),            # currently unused, kept for UI
    topn: int = Query(20, ge=1, le=50),

    budget_min: Optional[float] = Query(None),
    budget_max: Optional[float] = Query(None),

    # kept for UI; we surface them back in the response but don’t hard-filter on them yet
    min_profit: Optional[float] = Query(None),
    min_margin_pct: Optional[float] = Query(None),

    rating_min: Optional[int] = Query(None),
    rating_max: Optional[int] = Query(None),

    exclude_extinct: int = Query(1),        # currently not enforced (FUT.GG API doesn’t expose via our live call)
    exclude_low_liquidity: int = Query(1),  # placeholder
    exclude_anomalies: int = Query(1),      # placeholder

    debug: int = Query(0),
):
    """
    Returns a list of candidate deals:
      - Catalog pulled from `fut_players` (PLAYER_DATABASE_URL)
      - Live price pulled per card (PS for console, PC for pc)
      - Budget + rating filters applied on the *live* price + static rating
    """
    player_pool = getattr(request.app.state, "player_pool", None)
    if player_pool is None:
        raise HTTPException(500, "player DB pool not initialised")

    plat_collapsed = _collapse_platform(platform)
    price_plat = _price_platform(plat_collapsed)

    # Build a *catalog* query from fut_players (no dependency on a `price` column)
    where: List[str] = []
    params: List[Any] = []

    if rating_min is not None:
        params.append(int(rating_min)); where.append(f"rating >= ${len(params)}")
    if rating_max is not None:
        params.append(int(rating_max)); where.append(f"rating <= ${len(params)}")

    # Avoid loans / placeholders if you have such flags; otherwise this is harmless.
    where.append("(version IS NULL OR version NOT ILIKE '%loan%')")

    sql = f"""
      SELECT
        card_id::text     AS cid,
        name,
        version,
        rating,
        image_url,
        club,
        league
      FROM fut_players
      {"WHERE " + " AND ".join(where) if where else ""}
      ORDER BY rating DESC NULLS LAST, name ASC
      LIMIT 400
    """

    try:
        async with player_pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
    except Exception as e:
        logging.exception("trade_finder: catalog query failed")
        raise HTTPException(500, f"player catalog error: {e}")

    # Pull live prices concurrently
    async def _live_price(cid_int: int):
        try:
            px = await get_player_price(cid_int, price_plat)
            return int(px) if isinstance(px, (int, float)) else None
        except Exception:
            return None

    # Assemble tasks
    tasks = []
    cards: List[Dict[str, Any]] = []
    for r in rows:
        try:
            cid_int = int(r["cid"])
        except Exception:
            continue
        cards.append({
            "pid": cid_int,
            "name": r["name"],
            "version": r["version"],
            "rating": r["rating"],
            "image": r["image_url"],
            "club": r["club"],
            "league": r["league"],
        })
        tasks.append(_live_price(cid_int))

    prices = await asyncio.gather(*tasks, return_exceptions=False)

    # Apply budget filtering on the *live* price
    items: List[Dict[str, Any]] = []
    for card, px in zip(cards, prices):
        if px is None or px <= 0:
            continue
        if budget_min is not None and px < float(budget_min):
            continue
        if budget_max is not None and px > float(budget_max):
            continue

        items.append({
            **card,
            "platform": plat_collapsed,
            "prices": {"now": px},
            "edge": {
                "minProfit": _num(min_profit, None, float),
                "minMarginPct": _num(min_margin_pct, None, float),
                # flags just echoed back for now
                "excludeExtinct": int(exclude_extinct or 0),
                "excludeLowLiquidity": int(exclude_low_liquidity or 0),
                "excludeAnomalies": int(exclude_anomalies or 0),
            },
        })

    # Sort: simple heuristic — higher rating first, then cheaper to surface accessible cards
    items.sort(key=lambda x: (-int(x.get("rating") or 0), int(x["prices"]["now"])))
    items = items[:topn]

    if debug:
        return {
            "items": items,
            "meta": {
                "catalog_count": len(rows),
                "after_live_price_count": sum(1 for p in prices if isinstance(p, int) and p > 0),
                "returned": len(items),
                "platform_price_source": price_plat,
            },
        }

    return {"items": items}


@router.post("/trade-insight")
async def trade_insight(payload: Dict[str, Any]):
    """
    Return a lightweight, rules-based explanation (if you don't wire up LLMs).
    """
    deal = payload.get("deal") or {}
    name = deal.get("name", f"Card {deal.get('pid')}")
    px = (deal.get("prices") or {}).get("now")
    margin = (deal.get("edge") or {}).get("minMarginPct")
    profit = (deal.get("edge") or {}).get("minProfit")

    bits = []
    if isinstance(px, int): bits.append(f"live ≈ {px:,}c")
    if margin is not None: bits.append(f"target margin ≥ {margin}%")
    if profit is not None: bits.append(f"target profit ≥ {int(profit):,}c")

    text = f"{name}: matches your filters" + (f" ({', '.join(bits)})" if bits else ".")
    return {"insight": text}
