# app/services/futgg_history.py
from __future__ import annotations
import aiohttp, asyncio, json, re, time
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timezone

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "en-GB,en;q=0.9",
    "Referer": "https://www.fut.gg/",
    "Cache-Control": "no-cache",
}
_SEM = asyncio.Semaphore(5)

def _now_s() -> int:
    return int(time.time())

def _plat(p: str) -> str:
    p = (p or "").lower()
    if p in ("ps","ps4","ps5","playstation","console"): return "ps"
    if p in ("xbox","xb","xone","xsx"):                 return "xbox"
    if p in ("pc","origin","windows"):                  return "pc"
    return "ps"

async def _fetch_html(url: str) -> str:
    async with _SEM:
        async with aiohttp.ClientSession(headers=HEADERS) as s:
            for attempt in range(3):
                try:
                    async with s.get(url, timeout=15) as r:
                        if r.status == 200:
                            return await r.text()
                        if r.status in (403, 429):
                            raise RuntimeError(f"Blocked by Fut.GG: {r.status}")
                    await asyncio.sleep(0.8 * (attempt + 1))
                except Exception as e:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(0.8 * (attempt + 1))
    raise RuntimeError("failed to fetch Fut.GG page")

def _json_blobs_from_html(html: str) -> List[Any]:
    blobs: List[Any] = []
    # 1) Next.js payload
    m = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S|re.I)
    if m:
        try:
            blobs.append(json.loads(m.group(1)))
        except Exception:
            pass
    # 2) Any inline JSON-looking blocks (fallback)
    for mm in re.finditer(r'>(\{.*?\}|\[.*?\])<', html, re.S):
        txt = mm.group(1)
        if ("price" in txt or "coins" in txt or '"time"' in txt or '"timestamp"' in txt):
            try:
                blobs.append(json.loads(txt))
            except Exception:
                continue
    return blobs

def _walk(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)

def _norm_point(d: dict) -> Optional[Tuple[int, int]]:
    # try common key aliases
    t = d.get("t") or d.get("time") or d.get("timestamp") or d.get("x")
    v = d.get("price") or d.get("value") or d.get("coins") or d.get("y")
    if t is None or v is None:
        return None
    try:
        t = int(t)
    except Exception:
        return None
    if t > 2_000_000_000:  # likely ms -> s
        t //= 1000
    try:
        v = int(float(v))
    except Exception:
        return None
    return (t, v)

def _looks_recent_series(arr: Any) -> bool:
    if not isinstance(arr, list) or len(arr) < 8:  # need some density
        return False
    now_s = _now_s()
    cutoff = now_s - 3*24*3600 - 3600  # ~3d window + slack
    hits = 0
    for r in arr:
        if not isinstance(r, dict):
            return False
        p = _norm_point(r)
        if not p:
            continue
        if cutoff <= p[0] <= now_s:
            hits += 1
    return hits >= 5

def _best_series_from_blobs(blobs: List[Any]) -> List[Tuple[int,int]]:
    candidates: List[List[Tuple[int,int]]] = []
    for b in blobs:
        for node in _walk(b):
            if isinstance(node, list) and _looks_recent_series(node):
                buf: List[Tuple[int,int]] = []
                for r in node:
                    p = _norm_point(r)
                    if p:
                        buf.append(p)
                if buf:
                    candidates.append(buf)
    if not candidates:
        return []
    # pick the longest
    best = max(candidates, key=len)
    best.sort(key=lambda x: x[0])
    return best

async def fetch_futgg_history(card_id: int | str, platform: str = "ps", range_hint: str = "3d") -> List[Dict[str,int]]:
    """
    Return a 'candle-like' list for ~3 days:
    [{ ts, open, high, low, close }]
    """
    plat = _plat(platform)
    cid = str(card_id).strip()

    # Try a couple of URL shapes. The platform query helps Fut.GG pre-select a tab.
    candidates = [
        f"https://www.fut.gg/players/{cid}/?platform={plat}",
        f"https://www.fut.gg/players/{cid}?platform={plat}",
    ]

    series: List[Tuple[int,int]] = []
    last_err = None
    for url in candidates:
        try:
            html = await _fetch_html(url)
            blobs = _json_blobs_from_html(html)
            pts = _best_series_from_blobs(blobs)
            if pts:
                series = pts
                break
        except Exception as e:
            last_err = e
            continue

    if not series:
        # If you have a full slug URL from the frontend, you could try that too,
        # but we keep this helper simple.
        raise RuntimeError(f"Fut.GG series not found for {cid}/{plat}: {last_err or 'no series'}")

    # Map to flat OHLC candles to match your UI schema
    out = [{"ts": t, "open": v, "high": v, "low": v, "close": v} for (t, v) in series]
    return out
