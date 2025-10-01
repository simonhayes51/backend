from __future__ import annotations
import aiohttp
from typing import Dict, List, Tuple, Any
from datetime import datetime, timezone
from collections import defaultdict

FUTGG_PRICE_URL = "https://www.fut.gg/api/fut/player-prices/26/{card_id}"
TIMEOUT_SECS = 12
CACHE_TTL_SECS = 120

TF_TO_WINDOW_HOURS = {"today":24,"3d":72,"week":168,"month":720,"year":8760}
TF_TO_BUCKET_SECONDS = {"today":1800,"3d":3600,"week":7200,"month":21600,"year":86400}

GG_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en;q=0.9",
    "Origin": "https://www.fut.gg",
    "Referer": "https://www.fut.gg/",
}

_cache: Dict[str, Dict] = {}

def _now() -> int:
    import time; return int(time.time())

def _cache_get(k: str):
    v = _cache.get(k)
    if not v or _now() - v["at"] > CACHE_TTL_SECS:
        return None
    return v["points"]

def _cache_set(k: str, pts):
    _cache[k] = {"at": _now(), "points": pts}

def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms/1000, tz=timezone.utc).isoformat()

def _bucket(ms: int, sec: int) -> int:
    s = ms//1000; b = s - (s % sec); return b*1000

def _cutoff(now_ms: int, tf: str) -> int:
    return now_ms - TF_TO_WINDOW_HOURS.get(tf,24)*3600*1000

def _median(nums: List[int]) -> int:
    if not nums: return 0
    nums = sorted(nums); n = len(nums); m = n//2
    return nums[m] if n%2 else (nums[m-1]+nums[m])//2

async def _fetch_futgg(card_id: int) -> Dict[str, Any]:
    url = FUTGG_PRICE_URL.format(card_id=card_id)
    timeout = aiohttp.ClientTimeout(total=TIMEOUT_SECS)
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=GG_HEADERS) as sess:
            async with sess.get(url) as r:
                if r.status != 200:
                    return {}
                try:
                    return await r.json()
                except Exception:
                    return {}
    except Exception:
        return {}

async def get_price_history(card_id: int, platform: str = "ps", tf: str = "today") -> Dict[str, List[Dict]]:
    cache_key = f"{card_id}|{platform}|{tf}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return {"points": cached}

    data = await _fetch_futgg(card_id)
    auctions = data.get("completedAuctions") or (data.get("data") or {}).get("completedAuctions") or []
    pairs: List[Tuple[int,int]] = []
    for a in auctions:
        price = a.get("soldPrice") or a.get("price")
        sd = a.get("soldDate") or a.get("date")
        if not price or not sd:
            continue
        try:
            ts_ms = int(datetime.fromisoformat(str(sd).replace("Z","+00:00")).timestamp()*1000) \
                    if isinstance(sd, str) else int(sd)
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
        step = max(1, len(series)//600); series = series[::step]

    points = [{"t": _iso(ts), "price": pr} for ts, pr in series]
    _cache_set(cache_key, points)
    return {"points": points}
