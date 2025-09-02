# app/routers/trade_finder.py
from fastapi import APIRouter, Query, Request, HTTPException
from typing import Optional, List, Dict, Any, Literal
import asyncio
import logging
from math import floor

# Use your existing services for prices & history
from app.services.prices import get_player_price
from app.services.price_history import get_price_history

router = APIRouter()

EA_TAX = 0.05

# ----------------- helpers -----------------
def _plat_api(platform: str) -> Literal["ps", "xbox", "pc"]:
    s = (platform or "").lower()
    if s in ("console", "ps", "playstation", "sony"):  # we default console -> PS
        return "ps"
    if s in ("xb", "xbox"):
        return "xbox"
    return "pc"

def _plat_ui(platform: str) -> Literal["console", "pc"]:
    s = (platform or "").lower()
    return "pc" if s == "pc" else "console"

def _csv_set(s: Optional[str]) -> Optional[List[str]]:
    if not s:
        return None
    out = [x.strip().lower() for x in s.split(",") if x.strip()]
    return out or None

def _round_bin(x: float) -> int:
    """Round to a sensible FUT price tick (50 coins granularity works fine, keep it simple)."""
    if x <= 0:
        return 0
    return int(round(x / 50.0) * 50)

def _meets_profit(price_now: int, min_profit: Optional[float], min_margin_pct: Optional[float]) -> int:
    """
    Compute the minimal target sell that satisfies both:
      - net profit after tax >= min_profit
      - margin % >= min_margin_pct
    Return a rounded target sell (BIN tick).
    """
    if price_now <= 0:
        return 0

    # constraint A: margin
    want_margin = price_now * (1.0 + (min_margin_pct or 0) / 100.0) if min_margin_pct else 0.0

    # constraint B: net after tax >= min_profit
    # net = sell*(1-0.05) - price_now  >=  min_profit
    # sell >= (price_now + min_profit) / 0.95
    want_profit = ((price_now + float(min_profit or 0)) / (1.0 - EA_TAX)) if (min_profit and min_profit > 0) else 0.0

    target = max(want_margin, want_profit, 0.0)
    if target <= 0:
        # default nudge: 5% over now, if no constraints given
        target = price_now * 1.05

    return _round_bin(target)

def _pct(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None or a == 0:
        return None
    try:
        return round(((b - a) / a) * 100.0, 2)
    except Exception:
        return None

async def _change_pct_from_history(card_id: int, plat_api: str, hours: int) -> Optional[float]:
    """
    Best-effort change % over the given hours using today's history.
    If not enough points, return 0.0 (keeps UI happy).
    """
    try:
        series = await get_price_history(card_id, plat_api, "today")
        if not series:
            return 0.0
        # series items can be {t, price} or {t, v} etc. Normalize and filter the last <hours>.
        # We assume points are ~ every 15min. Just take the last N points: hours * 4.
        norm = []
        for p in series:
            t = p.get("t") or p.get("ts") or p.get("time")
            v = p.get("price") or p.get("v") or p.get("y")
            if t is not None and v is not None:
                norm.append((int(t), float(v)))
        if len(norm) < 2:
            return 0.0
        # grab last N points (approx)
        window_pts = norm[-max(2, hours * 4):]
        first = window_pts[0][1]
        last = window_pts[-1][1]
        return _pct(first, last) or 0.0
    except Exception:
        return 0.0

# ----------------- endpoint -----------------
@router.get("/trade-finder")
async def trade_finder(
    request: Request,
    platform: str = Query("console", pattern="^(console|pc)$"),
    timeframe: int = Query(24, ge=4, le=24),
    topn: int = Query(20, ge=1, le=50),

    budget_min: Optional[float] = Query(None),
    budget_max: Optional[float] = Query(None),
    min_profit: Optional[float] = Query(None),
    min_margin_pct: Optional[float] = Query(None),
    rating_min: Optional[int] = Query(None),
    rating_max: Optional[int] = Query(None),

    # optional text filters (comma separated)
    leagues: Optional[str] = Query(None, description="Comma separated league names"),
    nations: Optional[str] = Query(None, description="Comma separated nation names"),
    positions: Optional[str] = Query(None, description="Comma separated position codes like ST,CAM,CB"),

    # toggles we currently ignore in scoring but preserve in meta (future use)
    exclude_extinct: int = Query(1),
    exclude_low_liquidity: int = Query(1),
    exclude_anomalies: int = Query(1),

    # debug
    debug: int = Query(0),
):
    app = request.app
    player_pool = getattr(app.state, "player_pool", None)
    if player_pool is None:
        raise HTTPException(500, "Player DB pool not initialised")

    leagues_set = _csv_set(leagues)
    nations_set = _csv_set(nations)
    positions_set = _csv_set(positions)

    plat_api = _plat_api(platform)
    plat_ui = _plat_ui(platform)

    # Build WHERE for fut_players
    where = ["TRUE"]
    params: List[Any] = []

    if rating_min is not None:
        params.append(int(rating_min))
        where.append(f"rating >= ${len(params)}")
    if rating_max is not None:
        params.append(int(rating_max))
        where.append(f"rating <= ${len(params)}")

    # simple LIKE filters for leagues / nations / positions
    def _multi_like(col: str, values: List[str]) -> str:
        conds = []
        for v in values:
            params.append(f"%{v}%")
            conds.append(f"LOWER({col}) LIKE ${len(params)}")
        return "(" + " OR ".join(conds) + ")"

    if leagues_set:
        where.append(_multi_like("league", leagues_set))
    if nations_set:
        where.append(_multi_like("nation", nations_set))
    if positions_set:
        # match either position or altposition blob
        block = []
        block.append(_multi_like("position", positions_set))
        block.append(_multi_like("altposition", positions_set))
        where.append("(" + " OR ".join(block) + ")")

    # Pull a small catalog to price-check live
    sql = f"""
      SELECT card_id::text AS card_id,
             name, version, rating, position, league, image_url
        FROM fut_players
       WHERE {' AND '.join(where)}
    ORDER BY rating DESC NULLS LAST, name ASC
       LIMIT 200
    """

    try:
        async with player_pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
    except Exception as e:
        logging.exception("trade_finder: fut_players query failed")
        raise HTTPException(500, detail="Player catalog query failed")

    if not rows:
        return {"items": [], "meta": {"catalog_count": 0, "after_live_price_count": 0, "returned": 0, "platform_price_source": plat_api}}

    # Fetch live prices concurrently
    async def _price_for(card_id: int) -> Optional[int]:
        try:
            p = await get_player_price(card_id, plat_api)
            return int(p) if isinstance(p, (int, float)) else None
        except Exception:
            return None

    # First, build list of candidates
    candidates: List[Dict[str, Any]] = []
    for r in rows:
        try:
            cid = int(r["card_id"])
        except Exception:
            continue
        candidates.append({
            "card_id": cid,
            "player_id": cid,  # UI expects player_id for keys
            "name": r["name"],
            "version": r["version"],
            "rating": r["rating"],
            "position": r["position"],
            "league": r["league"],
            "image_url": r["image_url"],
        })

    # Add live prices
    prices = await asyncio.gather(*[_price_for(c["card_id"]) for c in candidates])
    for c, px in zip(candidates, prices):
        c["current_price"] = px

    # Filter by budget & extinct
    priced = []
    for c in candidates:
        px = c.get("current_price")
        if px is None or px <= 0:
            continue  # skip unknown price
        if budget_min is not None and px < budget_min:
            continue
        if budget_max is not None and px > budget_max:
            continue
        priced.append(c)

    # compute metrics & apply profit/margin constraints
    items: List[Dict[str, Any]] = []
    for c in priced:
        px = int(c["current_price"])
        target_sell = _meets_profit(px, min_profit, min_margin_pct)
        net = int(target_sell * (1.0 - EA_TAX) - px)
        margin_pct = round(100.0 * (target_sell - px) / px, 2) if px > 0 else 0.0

        # light placeholders (keep UI happy; cheap to compute)
        items.append({
            "player_id": c["player_id"],
            "card_id": c["card_id"],
            "name": c["name"],
            "version": c["version"],
            "rating": c["rating"],
            "position": c["position"],
            "league": c["league"],
            "image_url": c["image_url"],

            "platform": plat_ui,
            "timeframe_hours": int(timeframe),

            "current_price": px,
            "expected_sell": target_sell,
            "est_profit_after_tax": net,
            "margin_pct": margin_pct,

            # lightweight meta for chips
            "vol_score": 0.123,              # placeholder until we wire real volume/liquidity
            "change_pct_window": 0.0,        # set below with history fetch (batched)
            "seasonal_shift": None,          # can be None; UI checks for number
            "tags": [
                f"{c['version']}" if c.get("version") else "Base",
                f"{c['position']}" if c.get("position") else "Any",
            ],

            # keep toggles in edge, in case the UI or insight wants to show what was used
            "edge": {
                "minProfit": min_profit,
                "minMarginPct": min_margin_pct,
                "excludeExtinct": 1 if exclude_extinct else 0,
                "excludeLowLiquidity": 1 if exclude_low_liquidity else 0,
                "excludeAnomalies": 1 if exclude_anomalies else 0,
            },
        })

    # Sort by a simple score: prefer higher net profit then higher margin, then rating
    items.sort(key=lambda d: (d["est_profit_after_tax"], d["margin_pct"], d["rating"] or 0), reverse=True)
    items = items[:topn]

    # Get change % for the requested timeframe (cheap batch; cap to ~25 to keep it snappy)
    change_sample = items[: min(len(items), 25)]
    changes = await asyncio.gather(
        *[_change_pct_from_history(it["card_id"], plat_api, timeframe) for it in change_sample]
    )
    for it, cval in zip(change_sample, changes):
        # always provide a number to keep UI happy
        it["change_pct_window"] = float(cval if cval is not None else 0.0)

    payload = {
        "items": items,
        "meta": {
            "catalog_count": len(rows),
            "after_live_price_count": len(priced),
            "returned": len(items),
            "platform_price_source": plat_api,
        },
    }
    return payload

# ----------------- deal insight -----------------
@router.post("/trade-insight")
async def trade_insight(payload: Dict[str, Any]):
    """
    Simple rule-based explainer (no OpenAI dependency).
    Frontend expects { explanation: string }.
    """
    d = payload or {}
    name = d.get("name") or f"Card {d.get('card_id') or d.get('player_id') or ''}".strip()
    px = d.get("current_price")
    sell = d.get("expected_sell")
    net = d.get("est_profit_after_tax")
    margin = d.get("margin_pct")

    bits = []
    if isinstance(margin, (int, float)):
        bits.append(f"margin target ≈ {float(margin):.1f}%")
    if isinstance(net, (int, float)):
        bits.append(f"net profit ≈ {int(net):,}c after tax")
    if isinstance(px, (int, float)) and isinstance(sell, (int, float)):
        bits.append(f"buy ~{int(px):,}c → sell ~{int(sell):,}c")

    tags = d.get("tags") or []
    if tags:
        bits.append("tags: " + ", ".join(tags[:3]))

    msg = f"{name}: Candidate fits your filters"
    if bits:
        msg += " — " + "; ".join(bits) + "."
    else:
        msg += "."

    return {"explanation": msg}