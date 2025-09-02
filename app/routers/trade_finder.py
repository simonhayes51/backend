# app/routers/trade_finder.py
from fastapi import APIRouter, Query, Request, HTTPException
from typing import Optional, List, Dict, Any, Tuple
import asyncio
import logging
import math
import statistics
from datetime import datetime, timedelta

# we’ll use your existing services
from app.services.price_history import get_price_history
from app.services.prices import get_player_price

router = APIRouter()

# ---------- helpers ----------
def _collapse_platform(p: str) -> str:
    s = (p or "").lower()
    return "pc" if s in ("pc", "origin") else "console"

def _plat_for_live(p: str) -> str:
    s = (p or "").lower()
    if s in ("ps", "playstation", "console"): return "ps"
    if s in ("xbox", "xb"): return "xbox"
    if s in ("pc", "origin"): return "pc"
    return "ps"

def _num(v, default=None, cast=float):
    try:
        if v is None or v == "": return default
        return cast(v)
    except Exception:
        return default

def _parse_csv_words(s: Optional[str]) -> List[str]:
    if not s: return []
    return [w.strip().lower() for w in s.split(",") if w.strip()]

def _window_points(points: List[Dict[str, Any]], hours: int) -> List[int]:
    """Keep last N hours of 'today' series (t in ms, value in price|v|y). Returns list of ints (prices)."""
    if not points: return []
    try:
        now_ms = max(int(p.get("t") or p.get("ts") or 0) for p in points if (p.get("t") or p.get("ts")))
    except Exception:
        return []
    cutoff = now_ms - hours * 3600_000
    vals = []
    for p in points:
        t = p.get("t") or p.get("ts") or p.get("time")
        v = p.get("price") or p.get("v") or p.get("y")
        if t is None or v is None: continue
        if int(t) >= cutoff:
            try:
                vals.append(int(v))
            except Exception:
                continue
    return sorted(vals)

def _percentile(vals: List[int], pct: float) -> Optional[int]:
    if not vals: return None
    if pct <= 0: return min(vals)
    if pct >= 100: return max(vals)
    k = (len(vals) - 1) * (pct / 100.0)
    f = math.floor(k); c = math.ceil(k)
    if f == c: return vals[int(k)]
    d0 = vals[f] * (c - k)
    d1 = vals[c] * (k - f)
    return int(round(d0 + d1))

def _suggest_target_sell(history_vals: List[int]) -> Optional[int]:
    """
    Simple target model:
    - Use 70–80th percentile of the last window as a realistic “can achieve” price.
    - Nudge a bit upward if very tight distribution.
    """
    if not history_vals:
        return None
    p75 = _percentile(history_vals, 75)
    p80 = _percentile(history_vals, 80)
    base = p80 or p75 or (history_vals and int(statistics.mean(history_vals)))
    if not base:
        return None
    # If distribution is tight, a tiny premium is OK
    try:
        stdev = statistics.pstdev(history_vals) if len(history_vals) > 1 else 0
    except Exception:
        stdev = 0
    bump = 0
    if stdev and stdev / base < 0.03:
        bump = int(base * 0.01)
    return int(base + bump)

def _net_profit(buy_like_current: int, target_sell: int) -> int:
    # EA tax 5%, rounds down
    tax = int(math.floor(target_sell * 0.05))
    return int(target_sell - tax - buy_like_current)

def _margin_pct(buy_like_current: int, target_sell: int) -> float:
    if not buy_like_current:
        return 0.0
    tax = int(math.floor(target_sell * 0.05))
    return 100.0 * ((target_sell - tax - buy_like_current) / buy_like_current)

# -------------------------------- GET /api/trade-finder --------------------------------
@router.get("/trade-finder")
async def trade_finder(
    request: Request,
    platform: str = Query("console", pattern="^(console|pc)$"),
    timeframe: int = Query(24, ge=6, le=24),  # backend supports 6/12/24; UI can map 4->6
    topn: int = Query(20, ge=1, le=50),
    budget_min: Optional[float] = Query(None),
    budget_max: Optional[float] = Query(None),
    min_profit: Optional[float] = Query(None),           # net, after tax
    min_margin_pct: Optional[float] = Query(None),
    rating_min: Optional[int] = Query(None),
    rating_max: Optional[int] = Query(None),
    leagues: Optional[str] = Query(None, description="CSV of leagues terms"),
    nations: Optional[str] = Query(None),
    positions: Optional[str] = Query(None),
    exclude_extinct: int = Query(1),
    exclude_low_liquidity: int = Query(1),
    exclude_anomalies: int = Query(1),
    debug: int = Query(0),
):
    # Pools prepared by lifespan() in main.py
    pool = getattr(request.app.state, "pool", None)
    player_pool = getattr(request.app.state, "player_pool", None)
    if player_pool is None:
        raise HTTPException(500, "Player DB pool not initialised")

    plat_ui = _collapse_platform(platform)
    plat_live = _plat_for_live(plat_ui)

    # normalize numbers
    budget_min = _num(budget_min, None, float)
    budget_max = _num(budget_max, None, float)
    min_profit = _num(min_profit, None, float)
    min_margin_pct = _num(min_margin_pct, None, float)
    rating_min = _num(rating_min, None, int)
    rating_max = _num(rating_max, None, int)
    timeframe = 24 if timeframe not in (6, 12, 24) else timeframe

    leagues_q = _parse_csv_words(leagues)
    nations_q = _parse_csv_words(nations)
    positions_q = [p.upper() for p in _parse_csv_words(positions)]

    try:
        # ---------------- Candidate set from fut_players ----------------
        where = ["TRUE"]
        params: List[Any] = []
        if rating_min is not None:
            params.append(rating_min); where.append(f"rating >= ${len(params)}")
        if rating_max is not None:
            params.append(rating_max); where.append(f"rating <= ${len(params)}")

        # soft text filters
        def _ilike_any(col: str, terms: List[str]) -> Tuple[str, List[Any]]:
            if not terms: return ("", [])
            frags = []
            args = []
            for t in terms:
                args.append(f"%{t}%")
                frags.append(f"LOWER({col}) LIKE ${len(params)+len(args)}")
            return ("(" + " OR ".join(frags) + ")", args)

        lw, la = _ilike_any("league", leagues_q)
        if lw:
            where.append(lw); params.extend(la)
        nw, na = _ilike_any("nation", nations_q)
        if nw:
            where.append(nw); params.extend(na)

        if positions_q:
            params.append(positions_q)
            where.append(f"""(
                UPPER(position) = ANY(${len(params)})
                OR (
                  COALESCE(altposition, '') <> ''
                  AND EXISTS (
                    SELECT 1 FROM regexp_split_to_table(altposition, '[,;/|\\s]+') ap
                    WHERE UPPER(TRIM(ap)) = ANY(${len(params)})
                  )
                )
            )""")

        sql = f"""
            SELECT card_id, name, rating, version, image_url, club, league, nation, position, altposition
            FROM fut_players
            WHERE {' AND '.join(where)}
            ORDER BY rating DESC NULLS LAST
            LIMIT 400
        """

        async with player_pool.acquire() as conn:
            meta_rows = await conn.fetch(sql, *params)

        if not meta_rows:
            return {"items": [], "meta": {"catalog_count": 0, "after_live_price_count": 0, "returned": 0, "platform_price_source": plat_live}}

        # ---------------- Live price + history (batched) ----------------
        # We’ll fetch live price first, then history for those that pass budget.
        async def _live(cid: int):
            try:
                v = await get_player_price(cid, plat_live)
                return int(v) if isinstance(v, (int, float)) else None
            except Exception:
                return None

        # Run live price calls with concurrency
        sem = asyncio.Semaphore(10)
        async def _throttled_live(cid: int):
            async with sem:
                return await _live(cid)

        card_ids: List[int] = []
        metas: Dict[int, Dict[str, Any]] = {}
        for r in meta_rows:
            try:
                cid = int(r["card_id"])
            except Exception:
                continue
            metas[cid] = dict(r)
            card_ids.append(cid)

        live_prices = await asyncio.gather(*[_throttled_live(cid) for cid in card_ids])

        # Filter by budget + extinct if price None (treat None as unknown)
        candidates: List[int] = []
        after_live = 0
        for cid, price in zip(card_ids, live_prices):
            if price is None:
                continue
            after_live += 1
            if budget_min is not None and price < budget_min: continue
            if budget_max is not None and price > budget_max: continue
            candidates.append(cid)

        # Limit to reduce heavy history calls
        if len(candidates) > 120:
            candidates = candidates[:120]

        # Fetch histories (today) and compute expected sells
        async def _hist(cid: int):
            try:
                return await get_price_history(cid, plat_live, "today")
            except Exception:
                return []

        sem_hist = asyncio.Semaphore(8)
        async def _throttled_hist(cid: int):
            async with sem_hist:
                return await _hist(cid)

        histories = await asyncio.gather(*[_throttled_hist(cid) for cid in candidates])

        items: List[Dict[str, Any]] = []
        for cid, hist in zip(candidates, histories):
            cur = next((lp for c, lp in zip(card_ids, live_prices) if c == cid), None)
            if cur is None:
                continue
            # Build window from today series
            vals = _window_points(hist or [], timeframe)
            # crude liquidity proxy: points count in window
            liq = len(vals)

            if exclude_low_liquidity and liq < 6:
                # ~< 6 points in window is very illiquid (roughly < 90 mins of data @ 15min sampling)
                continue

            target = _suggest_target_sell(vals) or (cur + int(cur * 0.06))
            net = _net_profit(cur, target)
            margin = _margin_pct(cur, target)

            if min_margin_pct is not None and margin < float(min_margin_pct): 
                continue
            if min_profit is not None and net < float(min_profit):
                continue

            m = metas.get(cid, {})
            items.append({
                "player_id": cid,
                "card_id": cid,
                "platform": plat_ui,
                "timeframe_hours": timeframe,
                "name": m.get("name") or f"Card {cid}",
                "rating": m.get("rating"),
                "version": m.get("version"),
                "position": m.get("position"),
                "league": m.get("league"),
                "image_url": m.get("image_url"),
                "current_price": cur,
                "expected_sell": target,
                "est_profit_after_tax": net,
                "margin_pct": margin,
                # diagnostics (UI shows if present)
                "tags": ["TF", f"{timeframe}h"],
                "vol_score": round(min(1.0, liq / 96.0), 3),   # window density proxy (96 @ 24h, 24 @ 6h)
                "change_pct_window": _pct_change(vals) if vals else 0.0,
                "seasonal_shift": None,
            })

        # sort by best net profit, then margin
        items.sort(key=lambda x: (x["est_profit_after_tax"], x["margin_pct"]), reverse=True)
        items = items[:topn]

        return {
            "items": items,
            "meta": {
                "catalog_count": len(meta_rows),
                "after_live_price_count": after_live,
                "returned": len(items),
                "platform_price_source": plat_live,
            },
        }

    except Exception as e:
        logging.exception("trade_finder error")
        if debug:
            raise HTTPException(500, detail=str(e))
        raise HTTPException(500, "Internal error")

def _pct_change(vals: List[int]) -> float:
    if len(vals) < 2: return 0.0
    first, last = vals[0], vals[-1]
    if not first: return 0.0
    try:
        return round(100.0 * (last - first) / first, 2)
    except Exception:
        return 0.0

# -------------------------------- POST /api/trade-insight --------------------------------
@router.post("/trade-insight")
async def trade_insight(payload: Dict[str, Any]):
    deal = payload.get("deal") or {}
    name = deal.get("name", f"Card {deal.get('player_id') or deal.get('card_id')}")
    cur = deal.get("current_price")
    tgt = deal.get("expected_sell")
    tfh = deal.get("timeframe_hours") or 24
    margin = deal.get("margin_pct")
    vol = deal.get("vol_score")
    chg = deal.get("change_pct_window")
    tags = deal.get("tags") or []

    bits = []
    if isinstance(cur, (int, float)) and isinstance(tgt, (int, float)):
        tax = int(math.floor(tgt * 0.05))
        net = tgt - tax - cur
        bits.append(f"Now {int(cur):,}c → target ~{int(tgt):,}c (net ≈ {int(net):,}c after tax)")
    if isinstance(margin, (int, float)):
        bits.append(f"Margin ≈ {margin:.1f}%")
    if isinstance(vol, (int, float)):
        bits.append(f"Liquidity proxy {vol:.2f}")
    if isinstance(chg, (int, float)):
        arrow = "▲" if chg >= 0 else "▼"
        bits.append(f"{tfh}h change {arrow} {chg:.1f}%")

    if not bits:
        return {"insight": f"{name}: Candidate fits your filters. Adjust filters for tighter picks."}

    reason = []
    if isinstance(chg, (int, float)):
        if chg < -2:
            reason.append("recent dip creates rebound room")
        elif chg > 2:
            reason.append("momentum may sustain higher exit")
    if isinstance(vol, (int, float)) and vol < 0.15:
        reason.append("but liquidity is thin; expect slower fills")

    tail = f" ({'; '.join(reason)})" if reason else ""
    return {"insight": f"{name}: " + " • ".join(bits) + tail}