from __future__ import annotations
from typing import Dict, List, Tuple
from datetime import datetime, timezone
from collections import defaultdict

from app.futbin_client import get_player_url, fetch_recent_sales

# fut.gg's price-history API (the previous source here) is now
# Cloudflare-blocked, same reason the price and definition endpoints moved
# to futbin. futbin has no equivalent JSON history endpoint - its own price
# chart is a client-rendered Highcharts SVG - so instead we bucket its
# plain server-rendered sales history table (real timestamped sales) the
# same way the old fut.gg series was bucketed.
TF_TO_WINDOW_HOURS = {"today": 24, "3d": 72, "week": 168, "month": 720, "year": 8760}
TF_TO_BUCKET_SECONDS = {"today": 1800, "3d": 3600, "week": 7200, "month": 21600, "year": 86400}

CACHE_TTL_SECS = 120
_cache: Dict[str, Dict] = {}


def _now() -> int:
    import time
    return int(time.time())


def _cache_get(k: str):
    v = _cache.get(k)
    if not v or _now() - v["at"] > CACHE_TTL_SECS:
        return None
    return v["points"]


def _cache_set(k: str, pts):
    _cache[k] = {"at": _now(), "points": pts}


def _median(nums: List[int]) -> int:
    if not nums:
        return 0
    nums = sorted(nums)
    n = len(nums)
    m = n // 2
    return nums[m] if n % 2 else (nums[m - 1] + nums[m]) // 2


def _bucket(ts_ms: int, sec: int) -> int:
    s = ts_ms // 1000
    b = s - (s % sec)
    return b * 1000


def _cutoff(now_ms: int, tf: str) -> int:
    return now_ms - TF_TO_WINDOW_HOURS.get(tf, 24) * 3600 * 1000


async def get_price_history(card_id: int, platform: str = "ps", tf: str = "today") -> Dict[str, List[Dict]]:
    cache_key = f"{card_id}|{platform}|{tf}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return {"points": cached}

    player_url = await get_player_url(card_id)
    if not player_url:
        _cache_set(cache_key, [])
        return {"points": []}

    sales = await fetch_recent_sales(player_url, platform, limit=300)

    pairs: List[Tuple[int, int]] = []
    for s in sales:
        sd, price = s.get("soldDate"), s.get("soldPrice")
        if not sd or not price:
            continue
        try:
            ts_ms = int(datetime.fromisoformat(sd).timestamp() * 1000)
            pairs.append((ts_ms, int(price)))
        except Exception:
            continue

    if not pairs:
        _cache_set(cache_key, [])
        return {"points": []}

    pairs.sort(key=lambda x: x[0])
    now_ms = pairs[-1][0]
    pairs = [p for p in pairs if p[0] >= _cutoff(now_ms, tf)]

    by_bucket: Dict[int, List[int]] = defaultdict(list)
    bsec = TF_TO_BUCKET_SECONDS.get(tf, 1800)
    for ts, pr in pairs:
        by_bucket[_bucket(ts, bsec)].append(pr)

    series = sorted((b, _median(v)) for b, v in by_bucket.items())
    if len(series) > 600:
        step = max(1, len(series) // 600)
        series = series[::step]

    points = [
        {"t": datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat(), "price": pr}
        for ts, pr in series
    ]
    _cache_set(cache_key, points)
    return {"points": points}
