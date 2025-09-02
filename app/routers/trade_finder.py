# app/routers/trade_finder.py
from fastapi import APIRouter, Query, Request, HTTPException
from typing import Optional, List, Literal, Dict, Any, Tuple
import asyncio
import math
import statistics
from datetime import datetime, timedelta

# pull your shared services
from app.services.price_history import get_price_history
from app.services.prices import get_player_price

router = APIRouter()

EA_TAX = 0.05

# ---------- helpers ----------
def _collapse_platform(p: str) -> Literal["console","pc"]:
    s = (p or "").lower()
    return "pc" if s in ("pc", "origin") else "console"

def _coerce_float(v, default=None):
    try:
        if v is None or v == "": return default
        return float(v)
    except Exception:
        return default

def _coerce_int(v, default=None):
    try:
        if v is None or v == "": return default
        return int(v)
    except Exception:
        return default

def _pct(a: float, b: float) -> Optional[float]:
    try:
        if b == 0 or a is None or b is None:
            return None
        return 100.0 * (a - b) / b
    except Exception:
        return None

def _trimmed_mean(xs: List[float], drop=0.1) -> Optional[float]:
    """Robust ‘expected sell’ anchor: drop tails, mean the middle."""
    vals = [float(x) for x in xs if isinstance(x, (int, float))]
    n = len(vals)
    if n == 0:
        return None
    vals.sort()
    k = int(math.floor(n * drop))
    core = vals[k:n-k] if n - 2*k >= 1 else vals
    return float(sum(core) / len(core))

def _median(xs: List[float]) -> Optional[float]:
    vals = [float(x) for x in xs if isinstance(x, (int, float))]
    if not vals:
        return None
    return float(statistics.median(vals))

def _window_points(points: List[Dict[str, Any]], hours: int) -> List[Dict[str, Any]]:
    if not points: return []
    cutoff = int(datetime.utcnow().timestamp() * 1000) - int(hours*3600*1000)
    return [p for p in points if int(p.get("t") or p.get("ts") or p.get("time") or 0) >= cutoff]

def _flat_price(p: Dict[str, Any]) -> Optional[float]:
    return _coerce_float(p.get("price") or p.get("v") or p.get("y"), None)

def _price_taxed_out(sell_price: float) -> float:
    return float(sell_price) * (1.0 - EA_TAX)

def _tokenize_csv(s: Optional[str]) -> List[str]:
    if not s: return []
    return [x.strip() for x in s.split(",") if x.strip()]

# ---------- DB fetch ----------
async def _query_catalog(
    conn,
    *,
    rating_min: Optional[int],
    rating_max: Optional[int],
    leagues: List[str],
    nations: List[str],
    positions: List[str],
    budget_max: Optional[float],
) -> List[Dict[str, Any]]:
    where = ["card_id IS NOT NULL"]
    params: List[Any] = []

    if rating_min is not None:
        params.append(rating_min); where.append(f"rating >= ${len(params)}")
    if rating_max is not None:
        params.append(rating_max); where.append(f"rating <= ${len(params)}")

    # lightweight text filters (ILIKE any)
    def _any_ilike(col: str, values: List[str]) -> str:
        nonlocal params
        if not values: return ""
        ors = []
        for v in values:
            params.append(f"%{v}%")
            ors.append(f"{col} ILIKE ${len(params)}")
        return "(" + " OR ".join(ors) + ")"

    if leagues:
        where.append(_any_ilike("league", leagues))
    if nations:
        where.append(_any_ilike("nation", nations))
    if positions:
        # match in position or altposition list
        block = []
        for pos in positions:
            params.append(pos.upper())
            idx = len(params)
            block.append(
                f"(UPPER(position) = ${idx} OR (COALESCE(altposition,'') <> '' AND EXISTS ("
                f" SELECT 1 FROM regexp_split_to_table(altposition, '[,;/|\\s]+') ap "
                f" WHERE UPPER(TRIM(ap)) = ${idx})))"
            )
        where.append("(" + " OR ".join(block) + ")")

    sql = f"""
      SELECT card_id, name, version, rating, image_url, club, league, nation, position, altposition
      FROM fut_players
      WHERE {' AND '.join(w for w in where if w)}
      ORDER BY rating DESC NULLS LAST, name ASC
      LIMIT 400
    """
    rows = await conn.fetch(sql, *params)
    items = [dict(r) for r in rows]

    # optional budget_prune uses current price later (we don't know it yet)
    if budget_max is not None:
        # mark for budget check; we’ll drop after live prices attach
        for it in items: it["_budget_max"] = float(budget_max)

    return items

# ---------- deal scoring from history ----------
def _expected_sell_from_history(points: List[Dict[str, Any]]) -> Optional[float]:
    prices = [_flat_price(p) for p in points]
    prices = [x for x in prices if isinstance(x, (int, float, float)) and x is not None]
    if not prices:
        return None
    # median of last 4–24h is typically more stable than mean
    med = _median(prices)
    # blend with trimmed mean to not under/over react to spikes
    trm = _trimmed_mean(prices, drop=0.1)
    if med is None: return trm
    if trm is None: return med
    # weighted blend leans slightly to trimmed mean
    return round((0.6 * trm + 0.4 * med), 2)

def _change_pct(points: List[Dict[str, Any]]) -> Optional[float]:
    if len(points) < 2: return None
    first = _flat_price(points[0])
    last  = _flat_price(points[-1])
    if first is None or first == 0 or last is None:
        return None
    return round(100.0 * (last - first) / first, 2)

def _vol_score(points: List[Dict[str, Any]]) -> float:
    # proxy: number of samples clamped to [0,1]
    n = len([_flat_price(p) for p in points if _flat_price(p) is not None])
    return round(min(1.0, n / 96.0), 3)  # 96 points ~ full day @ 15-min bins

# ---------- API ----------
@router.get("/trade-finder")
async def trade_finder(
    request: Request,
    platform: str = Query("console", pattern="^(console|pc)$"),
    timeframe: int = Query(24, ge=4, le=24),
    topn: int = Query(20, ge=1, le=50),
    budget_max: Optional[float] = Query(None),
    min_profit: Optional[float] = Query(None),
    min_margin_pct: Optional[float] = Query(None),
    rating_min: Optional[int] = Query(None),
    rating_max: Optional[int] = Query(None),
    leagues: Optional[str] = Query(None, description="CSV of league fragments"),
    nations: Optional[str] = Query(None, description="CSV of nation fragments"),
    positions: Optional[str] = Query(None, description="CSV of positions (ST, CAM, …)"),
    exclude_extinct: int = Query(1),
    exclude_low_liquidity: int = Query(1),
    exclude_anomalies: int = Query(1),
    debug: int = Query(0),
):
    app_pool = getattr(request.app.state, "player_pool", None) or getattr(request.app.state, "pool", None)
    if app_pool is None:
        raise HTTPException(500, "DB pool not initialised")

    plat = _collapse_platform(platform)
    tf_hours = 4 if int(timeframe) <= 4 else 24

    # 1) candidate catalogue (DB)
    async with app_pool.acquire() as pconn:
        catalog = await _query_catalog(
            pconn,
            rating_min=_coerce_int(rating_min),
            rating_max=_coerce_int(rating_max),
            leagues=_tokenize_csv(leagues),
            nations=_tokenize_csv(nations),
            positions=_tokenize_csv(positions),
            budget_max=_coerce_float(budget_max),
        )

    # 2) Attach live prices (concurrent)
    async def _price_for(card_id: int) -> Optional[int]:
        try:
            return int(await get_player_price(int(card_id), "ps" if plat == "console" else "pc"))
        except Exception:
            return None

    live_prices = await asyncio.gather(
        *(_price_for(int(it["card_id"])) for it in catalog),
        return_exceptions=True
    )
    for it, v in zip(catalog, live_prices):
        it["current_price"] = int(v) if isinstance(v, (int, float)) else None

    # Budget prune now we know price
    if budget_max is not None:
        catalog = [it for it in catalog if it["current_price"] is not None and it["current_price"] <= float(budget_max)]

    # 3) Pull history window + compute expected sell, change %, vol
    async def _hist_for(card_id: int) -> Tuple[Optional[List[Dict[str, Any]]], Optional[List[Dict[str, Any]]]]:
        try:
            short = await get_price_history(int(card_id), "ps" if plat == "console" else "pc", "today")
        except Exception:
            short = []
        if tf_hours == 24:
            try:
                long = await get_price_history(int(card_id), "ps" if plat == "console" else "pc", "week")
            except Exception:
                long = short
        else:
            long = short
        return short, long

    histories = await asyncio.gather(
        *(_hist_for(int(it["card_id"])) for it in catalog),
        return_exceptions=True
    )

    items: List[Dict[str, Any]] = []
    after_live = 0

    for it, hv in zip(catalog, histories):
        if not isinstance(hv, tuple):
            continue
        short, long = hv
        after_live += 1

        # window for stats
        pts = _window_points(short or [], tf_hours)
        if not pts and long:
            pts = _window_points(long, tf_hours)

        expected_sell = _expected_sell_from_history(pts)
        cur = it.get("current_price")

        # extinct guard
        if exclude_extinct and (cur is None or cur == 0):
            continue

        # liquidity guard
        vol = _vol_score(pts)
        if exclude_low_liquidity and vol < 0.05:  # ~very few points
            continue

        # compute economics (only if we have both sides)
        if expected_sell is None or cur is None:
            continue

        net_out = _price_taxed_out(expected_sell)
        net_profit = int(round(net_out - cur))
        margin_pct = round(100.0 * (net_out - cur) / cur, 2) if cur else 0.0

        # user gates (based on *history-anchored* expectation)
        if min_profit is not None and net_profit < float(min_profit):
            continue
        if min_margin_pct is not None and margin_pct < float(min_margin_pct):
            continue

        # basic anomaly filter: if expected_sell is below current or absurdly high vs last price
        if exclude_anomalies:
            last = _flat_price(pts[-1]) if pts else None
            if last and expected_sell < 0.9 * last:  # expected below recent – likely a dip; still allow but be stricter
                if margin_pct < 5:
                    continue
            if last and expected_sell > 1.4 * last:  # big spike anchor – likely bad
                continue

        change_pct_window = _change_pct(pts) or 0.0

        items.append({
            "player_id": int(it["card_id"]),
            "card_id": int(it["card_id"]),
            "name": it["name"],
            "version": it.get("version"),
            "rating": it.get("rating"),
            "image_url": it.get("image_url"),
            "club": it.get("club"),
            "league": it.get("league"),
            "nation": it.get("nation"),
            "position": it.get("position"),
            "platform": plat,
            "timeframe_hours": tf_hours,

            # economics
            "current_price": cur,
            "expected_sell": int(round(expected_sell)),
            "est_profit_after_tax": net_profit,
            "margin_pct": margin_pct,

            # diagnostics / badges
            "vol_score": vol,
            "change_pct_window": change_pct_window,
            "tags": _deal_tags(vol, change_pct_window),
        })

    # 4) Sort by “quality”: higher profit, then margin, then rating
    items.sort(key=lambda d: (d["est_profit_after_tax"], d["margin_pct"], d.get("rating") or 0), reverse=True)
    items = items[:topn]

    return {
        "items": items,
        "meta": {
            "catalog_count": len(catalog),
            "after_live_price_count": after_live,
            "returned": len(items),
            "platform_price_source": "ps" if plat == "console" else "pc",
        },
    }

def _deal_tags(vol: float, change_pct_window: float) -> List[str]:
    tags = []
    if vol >= 0.5: tags.append("liquid")
    elif vol >= 0.2: tags.append("ok-liquidity")
    else: tags.append("thin")
    if change_pct_window is not None:
        if change_pct_window <= -2:
            tags.append("dip")
        elif change_pct_window >= 2:
            tags.append("rising")
    return tags

# ---------- “Why this deal?” ----------
@router.post("/trade-insight")
async def trade_insight(payload: Dict[str, Any]):
    d = payload.get("deal") or {}
    name = d.get("name", f"Card {d.get('player_id')}")
    cur = _coerce_float(d.get("current_price"), 0) or 0
    tgt = _coerce_float(d.get("expected_sell"), 0) or 0
    net = int(round(_price_taxed_out(tgt) - cur))
    margin = d.get("margin_pct")
    chg = d.get("change_pct_window")
    vol = d.get("vol_score") or 0.0

    bullets = []
    if margin is not None:
        bullets.append(f"~{margin:.1f}% net margin vs current")
    if chg is not None:
        arrow = "▼" if chg < 0 else "▲"
        bullets.append(f"{arrow} {abs(chg):.1f}% over window")
    if vol >= 0.5: bullets.append("good liquidity")
    elif vol >= 0.2: bullets.append("moderate liquidity")
    else: bullets.append("thin liquidity")

    if tgt and cur:
        bullets.append(f"target {int(tgt):,}c (EA tax {int(tgt*EA_TAX):,}c)")

    # quick sentiment
    if margin and margin >= 10 and (chg is not None and chg <= 0):
        verdict = "looks like a dip with healthy upside."
    elif margin and margin >= 7:
        verdict = "reasonable upside if undercut discipline is good."
    else:
        verdict = "tight spread — snipe only or skip."

    text = (
        f"{name}: Expected sell anchored to recent history, not a fixed margin.\n"
        f"• Net profit ≈ {net:,}c; " + " • ".join(bullets) + "\n"
        f"• Take-profit near {int(tgt):,}c; re-check spread before listing.\n"
        f"Overall: {verdict}"
    )
    return {"explanation": text}