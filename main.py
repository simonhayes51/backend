# app/routers/trade_finder.py
from fastapi import APIRouter, Query, Request, HTTPException
from typing import Optional, List, Literal, Dict, Any
import asyncio
import logging

from app.services.prices import get_player_price  # ✅ live FUT.GG fetch

router = APIRouter()

# Helpers
def _num(v, default=None, cast=float):
    try:
        if v is None or v == "": 
            return default
        return cast(v)
    except Exception:
        return default

def _collapse_platform(p: str) -> Literal["ps","xbox","pc"]:
    s = (p or "").lower()
    if s in ("pc","origin"): return "pc"
    if s in ("xbox","xb"): return "xbox"
    return "ps"   # default

# -------------------- Trade Finder --------------------
@router.get("/trade-finder")
async def trade_finder(
    request: Request,
    platform: str = Query("console", pattern="^(console|pc|ps|xbox)$"),
    timeframe: int = Query(24, ge=6, le=24),
    topn: int = Query(20, ge=1, le=50),
    budget_min: Optional[int] = Query(None),
    budget_max: Optional[int] = Query(None),
    rating_min: Optional[int] = Query(None),
    rating_max: Optional[int] = Query(None),
    min_profit: Optional[int] = Query(None),
    min_margin_pct: Optional[float] = Query(None),
    debug: int = Query(0),
):
    # ✅ use the players DB
    player_pool = getattr(request.app.state, "player_pool", None)
    if player_pool is None:
        raise HTTPException(500, "player_pool not initialised")

    plat = _collapse_platform(platform)

    try:
        # Candidate query: just ratings / metadata
        where = []
        params: List[Any] = []
        if rating_min is not None:
            params.append(rating_min); where.append(f"rating >= ${len(params)}")
        if rating_max is not None:
            params.append(rating_max); where.append(f"rating <= ${len(params)}")

        sql = f"""
          SELECT card_id::text AS cid, name, version, rating, image_url, club, league
          FROM fut_players
          {"WHERE " + " AND ".join(where) if where else ""}
          ORDER BY rating DESC NULLS LAST
          LIMIT 200
        """

        async with player_pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)

        # Fetch live prices concurrently
        async def enrich(r):
            cid = int(r["cid"])
            try:
                live_price = await get_player_price(cid, plat)
            except Exception as e:
                if debug: logging.warning("price fetch failed for %s: %s", cid, e)
                live_price = None
            if not isinstance(live_price, (int, float)) or live_price <= 0:
                return None

            # Apply budget filter here
            if budget_min and live_price < budget_min: 
                return None
            if budget_max and live_price > budget_max:
                return None

            return {
                "pid": cid,
                "name": r["name"],
                "version": r["version"],
                "rating": r["rating"],
                "image": r["image_url"],
                "club": r["club"],
                "league": r["league"],
                "platform": plat,
                "prices": {"now": live_price},
                "edge": {
                    "marginPct": min_margin_pct,
                    "minProfit": min_profit,
                },
            }

        tasks = [enrich(r) for r in rows]
        enriched = await asyncio.gather(*tasks)

        # Drop Nones, sort by rating as crude proxy, trim to topn
        items = [e for e in enriched if e]
        items = sorted(items, key=lambda x: x["rating"] or 0, reverse=True)[:topn]

        return {"items": items}

    except Exception as e:
        logging.exception("trade_finder error")
        if debug:
            raise HTTPException(500, detail=str(e))
        raise HTTPException(500, "Internal error")

# -------------------- Trade Insight --------------------
@router.post("/trade-insight")
async def trade_insight(payload: Dict[str, Any]):
    deal = payload.get("deal") or {}
    name = deal.get("name", f"Card {deal.get('pid')}")
    margin = (deal.get("edge") or {}).get("marginPct")
    bits = []
    if margin: bits.append(f"Target margin ≈ {margin}%")
    text = f"{name}: Candidate because it fits your filters. " + ("; ".join(bits) if bits else "Tweak filters for tighter picks.")
    return {"insight": text}
