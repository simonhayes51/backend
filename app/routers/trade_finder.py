# app/routers/trade_finder.py
from fastapi import APIRouter, Query, Request, HTTPException
from typing import Optional, List, Literal, Dict, Any
import logging

# ✅ mount this router under /api
router = APIRouter(prefix="/api")

# Helpers
def _num(v, default=None, cast=float):
    try:
        if v is None or v == "": return default
        return cast(v)
    except Exception:
        return default

def _collapse_platform(p: str) -> Literal["console","pc"]:
    s = (p or "").lower()
    return "pc" if s in ("pc", "origin") else "console"

@router.get("/trade-finder")
async def trade_finder(
    request: Request,
    platform: str = Query("console", pattern="^(console|pc)$"),
    timeframe: int = Query(24, ge=6, le=24),
    topn: int = Query(20, ge=1, le=50),
    budget_min: Optional[float] = Query(None),
    budget_max: Optional[float] = Query(None),
    min_profit: Optional[float] = Query(None),
    min_margin_pct: Optional[float] = Query(None),
    rating_min: Optional[int] = Query(None),
    rating_max: Optional[int] = Query(None),
    exclude_extinct: int = Query(1),
    exclude_low_liquidity: int = Query(1),
    exclude_anomalies: int = Query(1),
    debug: int = Query(0),
):
    # ✅ use the PLAYERS DB pool for fut_players
    player_pool = getattr(request.app.state, "player_pool", None)
    if player_pool is None:
        raise HTTPException(500, "player_pool not initialised")

    plat = _collapse_platform(platform)
    # Coerce numbers defensively
    budget_min = _num(budget_min, None, float)
    budget_max = _num(budget_max, None, float)
    min_profit = _num(min_profit, None, float)
    min_margin_pct = _num(min_margin_pct, None, float)
    rating_min = _num(rating_min, None, int)
    rating_max = _num(rating_max, None, int)
    exclude_extinct = 1 if int(exclude_extinct or 0) else 0
    exclude_low_liquidity = 1 if int(exclude_low_liquidity or 0) else 0
    exclude_anomalies = 1 if int(exclude_anomalies or 0) else 0

    try:
        # Basic candidate set from fut_players
        where = ["price IS NOT NULL"]
        params: List[Any] = []

        if rating_min is not None:
            params.append(rating_min); where.append(f"rating >= ${len(params)}")
        if rating_max is not None:
            params.append(rating_max); where.append(f"rating <= ${len(params)}")
        if budget_min is not None:
            params.append(budget_min); where.append(f"price >= ${len(params)}")
        if budget_max is not None:
            params.append(budget_max); where.append(f"price <= ${len(params)}")

        sql = f"""
          SELECT card_id::text AS cid, name, version, rating, image_url, club, league, nation, position, price
          FROM fut_players
          WHERE {' AND '.join(where)}
          ORDER BY rating DESC NULLS LAST
          LIMIT 200
        """

        async with player_pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)

        items = []
        for r in rows:
            now_price = r["price"] or 0
            items.append({
                "pid": int(r["cid"]),
                "name": r["name"],
                "version": r["version"],
                "rating": r["rating"],
                "image": r["image_url"],
                "club": r["club"],
                "league": r["league"],
                "platform": plat,
                "prices": {"now": now_price},
                "edge": {
                    "marginPct": min_margin_pct or None,
                    "minProfit": min_profit or None,
                    "filters": {
                        "exclude_extinct": bool(exclude_extinct),
                        "exclude_low_liquidity": bool(exclude_low_liquidity),
                        "exclude_anomalies": bool(exclude_anomalies),
                    }
                },
            })

        # TODO: replace with real scoring; for now just trim
        items = items[:topn]
        return {"items": items}

    except Exception as e:
        logging.exception("trade_finder error")
        if debug:
            raise HTTPException(500, detail=str(e))
        raise HTTPException(500, "Internal error")

@router.post("/trade-insight")
async def trade_insight(payload: Dict[str, Any]):
    deal = payload.get("deal") or {}
    name = deal.get("name", f"Card {deal.get('pid')}")
    margin = (deal.get("edge") or {}).get("marginPct")
    bits = []
    if margin: bits.append(f"Target margin ≈ {margin}%")
    text = f"{name}: Candidate because it fits your filters. " + ("; ".join(bits) if bits else "Tweak filters for tighter picks.")
    return {"insight": text}
