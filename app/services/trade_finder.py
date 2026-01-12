# app/services/trade_finder.py
from __future__ import annotations

import asyncio
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone, timedelta
import math

import asyncpg

from app.services.price_history import get_price_history
from app.services.prices import get_player_price
from app.services.seasonality import seasonal_profile, rewards_bias

# ----------------- helpers -----------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

def _round_sell(price: int) -> int:
    if price <= 0:
        return 0
    return int(math.floor(price / 50) * 50)

def _net_after_tax(sell_price: int) -> int:
    # EA tax 5%
    return int(math.floor(sell_price * 0.95))

def _percentile(values: List[int], pct: float) -> int:
    if not values:
        return 0
    xs = sorted(values)
    k = (len(xs) - 1) * pct
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return xs[int(k)]
    d0 = xs[f] * (c - k)
    d1 = xs[c] * (k - f)
    return int(d0 + d1)

def _slope_pct(prices: List[int]) -> float:
    n = len(prices)
    if n < 5:
        return 0.0
    xbar = (n - 1) / 2
    ybar = sum(prices) / n
    num = sum((i - xbar) * (p - ybar) for i, p in enumerate(prices))
    den = sum((i - xbar)**2 for i in range(n)) or 1
    slope = num / den
    return (slope / max(1, ybar)) * 100.0

def _down_ratio(prices: List[int]) -> float:
    if len(prices) < 5:
        return 0.0
    steps = sum(1 for i in range(1, len(prices)) if prices[i] < prices[i-1])
    return steps / (len(prices) - 1)

def _robust_trim(prices: List[int]) -> tuple[List[int], dict]:
    """
    Removes extreme outliers via IQR + MAD; returns cleaned prices + debug.
    """
    if len(prices) < 8:
        return prices, {"removed": 0, "method": "none"}
    xs = sorted(p for p in prices if p > 0)
    n = len(xs)
    q1 = xs[int(0.25*(n-1))]
    q3 = xs[int(0.75*(n-1))]
    iqr = max(1, q3 - q1)
    lo_iqr = q1 - 1.5 * iqr
    hi_iqr = q3 + 1.5 * iqr
    xs_iqr = [p for p in xs if lo_iqr <= p <= hi_iqr]

    import statistics as st
    base = xs_iqr or xs
    med = st.median(base)
    abs_dev = [abs(p - med) for p in base]
    mad = st.median(abs_dev) or 1
    xs_mad = [p for p in base if abs(p - med) / mad <= 6]

    removed = len(prices) - len(xs_mad)
    return xs_mad or xs, {"removed": removed, "method": "iqr+mad", "q1": q1, "q3": q3, "mad": mad, "median": med}

@dataclass
class Deal:
    player_id: int
    card_id: Optional[int]
    name: str
    rating: int
    position: Optional[str]
    league: Optional[str]
    nation: Optional[str]
    version: Optional[str]
    image_url: Optional[str]

    platform: str
    timeframe_hours: int

    current_price: int
    baseline_price: int
    expected_sell: int
    est_profit_after_tax: int
    margin_pct: float

    vol_score: float
    change_pct_window: float
    sample_count: int

    tags: List[str]

    # Optional: seasonal chip value (not required by dataclass defaults)
    seasonal_shift: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["margin_pct"] = round(self.margin_pct, 2)
        d["vol_score"] = round(self.vol_score, 3)
        d["change_pct_window"] = round(self.change_pct_window, 2)
        if d.get("seasonal_shift") is not None:
            d["seasonal_shift"] = round(float(d["seasonal_shift"]), 1)
        return d

async def _fetch_basic_players(
    pool: asyncpg.Pool,
    rating_min: int,
    rating_max: int,
    includes: Dict[str, List[str]],
    limit: int = 400,
) -> List[asyncpg.Record]:
    wheres = ["rating BETWEEN $1 AND $2"]
    params: List[Any] = [rating_min, rating_max]
    idx = 3

    if includes.get("leagues"):
        wheres.append(f"league = ANY(${idx})")
        params.append(includes["leagues"]); idx += 1
    if includes.get("nations"):
        wheres.append(f"nation = ANY(${idx})")
        params.append(includes["nations"]); idx += 1
    if includes.get("positions"):
        wheres.append(f"position = ANY(${idx})")
        params.append(includes["positions"]); idx += 1

    sql = f"""
        SELECT id as player_id, card_id, name, rating, position, league, nation, version, image_url
        FROM fut_players
        WHERE {' AND '.join(wheres)}
        ORDER BY rating DESC, id ASC
        LIMIT {int(limit)}
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return rows

async def _compute_deal_for_player(
    pool: asyncpg.Pool,
    row: asyncpg.Record,
    platform: str,
    timeframe_hours: int,
    min_margin_pct: float,
) -> Optional[Deal]:
    player_id = int(row["player_id"]); name = row["name"]; rating = int(row["rating"])

    pill = "3d" if timeframe_hours == 24 else "1d"
    try:
        hist = await get_price_history(player_id, pill=pill, platform=platform)
    except Exception:
        hist = []

    if not hist:
        return None

    now = _now_utc()
    window_start = now - timedelta(hours=timeframe_hours)
    pts = [p for p in hist if datetime.fromtimestamp(p["t"]/1000, tz=timezone.utc) >= window_start and p.get("price")]
    if len(pts) < 4:
        return None

    raw_prices = [int(p["price"]) for p in pts if int(p["price"]) > 0]
    if not raw_prices:
        return None

    prices, dbg = _robust_trim(raw_prices)
    if len(prices) < 4:
        return None

    # live price with fallback
    try:
        live = await get_player_price(player_id, platform=platform)
        current_price = int(live or 0)
    except Exception:
        current_price = prices[-1]
    if current_price <= 0:
        return None

    # mislist / extreme undercut guard
    import statistics as st
    median_win = int(st.median(prices))
    mad_win = max(1, st.median([abs(p - median_win) for p in prices]))
    is_extreme_undercut = (median_win - current_price) > max(3*mad_win, int(0.25*median_win))
    recent = prices[-10:] if len(prices) >= 10 else prices
    confirm_count = sum(1 for p in recent if p <= int(current_price * 1.1))
    if is_extreme_undercut and confirm_count < 2:
        return None

    # baseline / profit
    baseline = _percentile(prices, 0.65)
    expected_sell = _round_sell(baseline)
    est_profit = _net_after_tax(expected_sell) - current_price
    margin_pct = 0.0 if baseline <= 0 else (baseline - current_price) / baseline * 100.0

    # volatility & directional change
    mean_price = sum(prices) / len(prices)
    var = sum((p - mean_price)**2 for p in prices) / max(1, (len(prices)-1))
    stdev = math.sqrt(max(0.0, var))
    vol_score = 0.0 if mean_price == 0 else stdev / mean_price
    change_pct = 0.0 if prices[0] == 0 else (prices[-1] - prices[0]) / prices[0] * 100.0

    tags: List[str] = []
    p25 = _percentile(prices, 0.25)
    if current_price <= p25:
        tags.append("Undercut")

    # 7d floor / crash context
    try:
        hist24 = await get_price_history(player_id, pill="3d", platform=platform)
        hist7  = await get_price_history(player_id, pill="7d", platform=platform)
    except Exception:
        hist24, hist7 = [], []

    def _slice_since(hist, hours):
        if not hist: return []
        start = now - timedelta(hours=hours)
        return [int(p.get("price") or 0) for p in hist if int(p.get("price") or 0) > 0 and datetime.fromtimestamp(p["t"]/1000, tz=timezone.utc) >= start]

    p4  = _slice_since(hist24, 4)  or prices[-max(4, len(prices)//6):]
    p24 = _slice_since(hist24, 24) or prices
    p7  = _slice_since(hist7, 168) or prices

    def _chg(xs):
        return 0.0 if len(xs) < 2 or xs[0]==0 else (xs[-1]-xs[0])/xs[0]*100.0

    chg4, chg24, chg7 = _chg(p4), _chg(p24), _chg(p7)
    slope24 = _slope_pct(p24)
    down24  = _down_ratio(p24)
    p65 = _percentile(prices, 0.65)
    compressed_low = (p65 - p25) / max(1, p65) <= 0.07

    crash_ongoing = (chg24 <= -10 and chg7 <= -15 and slope24 < 0 and down24 >= 0.65 and compressed_low)
    panic_dip     = (chg24 <= -6 and chg7 > -12) or (chg4 <= -4 and slope24 >= -0.5)

    if crash_ongoing:
        tags.append("Crash Ongoing")
        return None  # block; or require higher margin if you prefer
    elif panic_dip:
        tags.append("Panic Dip")
        expected_sell = _round_sell(int(baseline * 0.96))
        est_profit = _net_after_tax(expected_sell) - current_price

    # 7d low check (safe floor)
    try:
        hist7_all = await get_price_history(player_id, pill="7d", platform=platform)
        low7 = min(int(p.get("price") or 0) for p in hist7_all if int(p.get("price") or 0) > 0)
        if low7 > 0 and current_price <= int(low7 * 1.03):
            tags.append("Safe Floor")
    except Exception:
        pass

    if rating >= 84:
        tags.append("Fodder Candidate")
    if vol_score >= 0.08:
        tags.append("High Volatility")

    # seasonality / rewards-day context
    prof = await seasonal_profile(pool, player_id, platform)
    key = f"{now.weekday()}:{now.hour}"
    seasonal_shift = prof.get(key, 0.0) if prof else 0.0
    rb = await rewards_bias(pool, now)

    if rb > 0 and seasonal_shift <= -1.0:
        expected_sell = _round_sell(int(expected_sell * 0.97))
        est_profit = _net_after_tax(expected_sell) - current_price
        tags.append("Rewards Dip Likely")
    elif rb == 0 and seasonal_shift >= 1.0 and margin_pct >= (min_margin_pct - 1.0):
        tags.append("Strong Hour")

    # Final thresholds gate (after adjustments)
    if margin_pct < min_margin_pct:
        return None

    return Deal(
        player_id=player_id,
        card_id=row.get("card_id"),
        name=name,
        rating=rating,
        position=row.get("position"),
        league=row.get("league"),
        nation=row.get("nation"),
        version=row.get("version"),
        image_url=row.get("image_url"),
        platform=platform,
        timeframe_hours=timeframe_hours,
        current_price=current_price,
        baseline_price=int(baseline),
        expected_sell=expected_sell,
        est_profit_after_tax=est_profit,
        margin_pct=margin_pct,
        vol_score=vol_score,
        change_pct_window=change_pct,
        sample_count=len(prices),
        tags=tags,
        seasonal_shift=seasonal_shift,
    )

async def find_deals(
    pool: asyncpg.Pool,
    *,
    platform: str = "console",
    timeframe_hours: int = 24,
    budget_max: int = 150000,
    min_profit: int = 1500,
    min_margin_pct: float = 8.0,
    rating_min: int = 75,
    rating_max: int = 93,
    includes: Optional[Dict[str, List[str]]] = None,
    limit_players: int = 400,
    topn: int = 50,
) -> List[Dict[str, Any]]:
    includes = includes or {}
    rows = await _fetch_basic_players(pool, rating_min, rating_max, includes, limit=limit_players)

    sem = asyncio.Semaphore(12)
    deals: List[Deal] = []

    async def worker(r):
        async with sem:
            d = await _compute_deal_for_player(pool, r, platform, timeframe_hours, min_margin_pct)
            if not d:
                return
            if d.current_price > budget_max:
                return
            if d.est_profit_after_tax < min_profit:
                return
            deals.append(d)

    await asyncio.gather(*(worker(r) for r in rows))

    deals.sort(key=lambda d: (d.est_profit_after_tax, d.margin_pct), reverse=True)
    return [d.to_dict() for d in deals[:topn]]

def cache_key_from_filters(filters: Dict[str, Any]) -> str:
    import hashlib, json
    blob = json.dumps(filters, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()
