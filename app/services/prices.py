# app/services/prices.py
# Built-in lightweight price fetcher that mirrors what your Player-Search UI uses.
# No DB table required. Uses FUT.GG's JSON to get current price.
#
# Usage from routers:
#   from app.services.prices import get_player_price
#   price = await get_player_price(card_id, platform)  # platform kept for API parity

import aiohttp

HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (compatible; SquadBuilder/1.0)"
}

API_URL_TEMPLATE = "https://www.fut.gg/api/fut/player-prices/25/{card_id}"

async def get_player_price(card_id: int, platform: str = "ps") -> int:
    """Return current BIN price for a card.
    Note: FUT.GG currentPrice is not platform-specific in this endpoint; we keep the
    platform arg for API compatibility with your frontend/routes.
    On failure, returns 0.
    """
    url = API_URL_TEMPLATE.format(card_id=card_id)
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout, headers=HEADERS) as sess:
            async with sess.get(url) as r:
                if r.status != 200:
                    return 0
                data = await r.json()
                price = (data.get("data") or {}).get("currentPrice") or {}
                val = price.get("price")
                if isinstance(val, (int, float)):
                    return int(val)
                # Sometimes numbers come back as strings
                try:
                    return int(str(val).replace(",", "")) if val is not None else 0
                except Exception:
                    return 0
    except Exception:
        return 0
