# app/services/price_history.py
# FUT.GG-only price history for the chart, derived from completedAuctions.
# Returns: {"points": [{"t": ISO_UTC, "price": int}, ...]}

from __future__ import annotations

import aiohttp
from typing import Dict, List, Tuple, Any
from datetime import datetime, timezone
from collections import defaultdict

# ✅ Use v26 to match the rest of the app
FUTGG_PRICE_URL = "https://www.fut.gg/api/fut/player-prices/26/{card_id}"
TIMEOUT_SECS = 12
CACHE_TTL_SECS = 120  # short cache; we're showing recent trends

TF_TO_WINDOW_HOURS = {
    "today": 24,
    "3d": 72,
    "week": 24 * 7,
    "month": 24 * 30,
    "year": 24 * 365,
}

TF_TO_BUCKET_SECONDS = {
    "today": 30 * 60,       # 30 minutes
    "3d":    60 * 60,       # 1 hour
    "week":  2 * 60 * 60,   # 2 hours
    "month": 6 * 60 * 60,   # 6 hours
    "year":  24 * 60 * 60,  # 1 day
}

GG_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en;q=0.9",
    "Origin": "https://www.fut.gg",
    "Referer": "https://www.fut.gg/",
}

# -------- tiny in-memory cache --------
_cache: Dict[str, Dict] = {}

def _now() -> int:
    import time
    return int(time.time())

def _cache_get(key: str):
    v = _cache.get(key)
    if not v or _now() - v["at"] > CACHE_TTL_SECS:
        return None
    return v["points"]

def _cache_set(key: str, points):
    _cache[key] = {"at": _now(), "points": points}

# -------- helpers --------
def _iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()

def _bucket(ts_ms: int, bucket_seconds: int) -> int:
    s = ts_ms // 1000
    b = s - (s % bucket_seconds)
    return b * 1000  # back to ms

def _window_cutoff(now_ms: int, tf: str) -> int:
    hours = TF_TO_WINDOW_HOURS.get(tf, 24)
    return now_ms - hours * 3600 * 1000

def _median(nums: List[int]) -> int:
    if not nums:
        return 0
    nums = sorted(nums)
    mid = len(nums) // 2
    return nums[mid] if len(nums) % 2 else (nums[mid - 1] + nums[mid]) // 2

# -------- FUT.GG fetch --------
async def _fetch_futgg_data(card_id: int) -> Dict[str, Any]:
    url = FUTGG_PRICE_URL.format(card_id=card_id)
    timeout = aiohttp.ClientTimeout(total=TIMEOUT_SECS)
    async with aiohttp.ClientSession(timeout=timeout, headers=GG_HEADERS) as sess:
        async with sess.get(url) as r:
            # If FUT.GG blocks or has no data, return empty rather than raise
            if r.status != 200:
                return {}
            try:
                return await r.json()
            except Exception:
                return {}

# -------- public API --------
async def get_price_history(card_id: int, platform: str = "ps", tf: str = "today") -> Dict[str, List[Dict[str, int]]]:
    """
    Build history from FUT.GG completed auctions, bucketed per timeframe.
    Shape: {"points":[{"t": ISO_UTC, "price": int}, ...]}
    """
    cache_key = f"{card_id}|{platform}|{tf}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return {"points": cached}

    data = await _fetch_futgg_data(card_id)
    if not data:
        _cache_set(cache_key, [])
        return {"points": []}

    # FUT.GG sometimes nests auctions under data.completedAuctions
    auctions = data.get("completedAuctions") or (data.get("data") or {}).get("completedAuctions") or []

    # Parse auctions -> (ts_ms, price)
    pairs: List[Tuple[int, int]] = []
    for a in auctions:
        price = a.get("soldPrice") or a.get("price")
        sd = a.get("soldDate") or a.get("date")
        if not price or not sd:
            continue
        try:
            # FUT.GG uses ISO strings with 'Z'; guard both second + ms formats
            if isinstance(sd, str):
                dt = datetime.fromisoformat(sd.replace("Z", "+00:00"))
                ts_ms = int(dt.timestamp() * 1000)
            else:
                # if already ms epoch
                ts_ms = int(sd)
            pairs.append((ts_ms, int(price)))
        except Exception:
            continue

    if not pairs:
        _cache_set(cache_key, [])
        return {"points": []}

    pairs.sort(key=lambda x: x[0])
    now_ms = pairs[-1][0]
    cutoff = _window_cutoff(now_ms, tf)
    pairs = [p for p in pairs if p[0] >= cutoff]

    # Bucket and take median per bucket to smooth noise
    bucket_seconds = TF_TO_BUCKET_SECONDS.get(tf, 30 * 60)
    by_bucket: Dict[int, List[int]] = defaultdict(list)
    for ts, price in pairs:
        by_bucket[_bucket(ts, bucket_seconds)].append(price)

    series: List[Tuple[int, int]] = [(b, _median(vals)) for b, vals in by_bucket.items()]
    series.sort(key=lambda x: x[0])

    # Light downsampling cap (keep payload reasonable)
    if len(series) > 600:
        step = max(1, len(series) // 600)
        series = series[::step]

    points = [{"t": _iso(ts), "price": price} for ts, price in series]
    _cache_set(cache_key, points)
    return {"points": points}
