# app/services/price_history.py
import re
import json
import aiohttp
from bs4 import BeautifulSoup
from typing import List, Dict

HEADERS = {
    "Accept": "text/html,application/xhtml+xml",
    "User-Agent": "Mozilla/5.0 (compatible; FUTDashboard/1.0)",
}

FUTBIN_PLAYER_URL = "https://www.futbin.com/25/player/{card_id}"

# Simple timeframe gate (you can refine server-side if you want)
TF_TO_HOURS = {
    "today": 24,
    "3d": 72,
    "week": 24 * 7,
    "month": 24 * 30,
    "year": 24 * 365,
}

async def _fetch_html(url: str) -> str:
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout, headers=HEADERS) as sess:
        async with sess.get(url) as r:
            r.raise_for_status()
            return await r.text()

def _extract_series_data(html: str) -> List[List[float]]:
    """
    FUTBIN renders a Highcharts config in inline scripts. We extract the first
    'data: [[ts, price], ...]' array we find. Your bot used the 2nd highcharts
    wrapper; this regex approach avoids DOM traversal on the server.
    """
    # Narrow to the price graph container if present for faster regex (optional)
    # Otherwise just search whole html.
    # Find the longest plausible 'data: [...]'
    data_blocks = re.findall(r"data\s*:\s*(\[\s*\[.*?\]\s*\])", html, flags=re.DOTALL)
    if not data_blocks:
        return []

    # Pick the block with the most pairs (usually the price series)
    best = max(data_blocks, key=lambda s: s.count("],"))
    # Clean trailing commas, then JSON-load
    # The content should be pure JSON-like [[1693000000000, 18500], ...]
    cleaned = re.sub(r",\s*([\]\}])", r"\1", best)
    try:
        arr = json.loads(cleaned)
        # Ensure each is [timestamp(ms|s), price]
        series = []
        for row in arr:
            if not (isinstance(row, list) and len(row) >= 2):
                continue
            ts, price = row[0], row[1]
            if isinstance(ts, (int, float)) and isinstance(price, (int, float)):
                # Convert seconds → ms if needed
                if ts < 10_000_000_000:  # likely seconds
                    ts = int(ts * 1000)
                series.append([int(ts), int(price)])
        return series
    except Exception:
        return []

def _slice_by_timeframe(series: List[List[float]], tf: str) -> List[List[float]]:
    if not series:
        return series
    hours = TF_TO_HOURS.get(tf, 24)
    # Keep last N hours of data
    # series is chronological; if not, sort by ts
    series = sorted(series, key=lambda x: x[0])
    cutoff = series[-1][0] - hours * 3600 * 1000
    return [row for row in series if row[0] >= cutoff]

async def get_price_history(card_id: int, platform: str = "ps", tf: str = "today") -> Dict:
    """
    Returns { points: [{ t: ISO_STRING, price: int }, ...] }
    Note: FUTBIN charts are per-console; we’re scraping the default console line used on page.
    If you later need strict PS/Xbox selection, target the console toggle script block.
    """
    url = FUTBIN_PLAYER_URL.format(card_id=card_id)
    html = await _fetch_html(url)
    soup = BeautifulSoup(html, "html.parser")

    # Try to prefer the second highcharts block like your bot did by scoping:
    # but regex already grabbed the richest data block above.
    series = _extract_series_data(html)
    series = _slice_by_timeframe(series, tf)

    points = [{"t": __ts_to_iso(ts), "price": price} for ts, price in series]
    return {"points": points}

def __ts_to_iso(ts_ms: int) -> str:
    # Keep it lightweight; no tz conversions here (ISO in UTC)
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()
