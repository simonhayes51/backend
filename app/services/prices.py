# app/services/prices.py
# Fast current price from FUT.GG with robust headers.
# Falls back to the most recent price from price_history if the live endpoint fails.

from __future__ import annotations

import aiohttp
from typing import Optional
from app.services.price_history import get_price_history

GG_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en;q=0.9",
    "Origin": "https://www.fut.gg",
    "Referer": "https://www.fut.gg/",
}

API_URL_TEMPLATE = "https://www.fut.gg/api/fut/player-prices/25/{card_id}"

async def _live_price(card_id: int) -> int:
    url = API_URL_TEMPLATE.format(card_id=card_id)
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout, headers=GG_HEADERS) as sess:
        async with sess.get(url) as r:
            if r.status != 200:
                return 0
            data = await r.json()
            cur = (data.get("data") or {}).get("currentPrice") or {}
            val = cur.get("price")
            if isinstance(val, (int, float)):
                return int(val)
            try:
                return int(str(val).replace(",", "")) if val is not None else 0
            except Exception:
                return 0

async def _fallback_from_history(card_id: int) -> int:
    # Try the latest point from the "today" history
    try:
        hist = await get_price_history(card_id, "ps", "today")
        pts = hist.get("points") or []
        if pts:
            last = next((p for p in reversed(pts) if isinstance(p.get("price"), int)), None)
            return int(last["price"]) if last else 0
        return 0
    except Exception:
        return 0

async def get_player_price(card_id: int, platform: str = "ps") -> int:
    """
    Return current BIN price for a card (PS by default).
    If live endpoint fails, fall back to last point from history.
    """
    price = await _live_price(card_id)
    if price > 0:
        return price
    # fallback
    return await _fallback_from_history(card_id)