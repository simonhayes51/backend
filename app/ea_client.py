import os
import httpx
from typing import Optional

EA_BASE = "https://utas.mob.v5.prd.futc-ext.gcp.ea.com/ut/game/fc26"

class ExpiredEA(Exception): pass
class RateLimitedEA(Exception):
    def __init__(self, retry_after: Optional[int]): self.retry_after = retry_after

def get_configured_sid() -> Optional[str]:
    """Returns the configured EA session token, or None if not set."""
    return os.getenv("EA_X_UT_SID")

async def ea_get(path: str, sid: str, params=None):
    url = f"{EA_BASE}/{path.lstrip('/')}"
    headers = {
        "x-ut-sid": sid,
        "Origin": "https://www.ea.com",
        "Referer": "https://www.ea.com/",
        "User-Agent": "Mozilla/5.0",
        "Accept": "*/*",
    }
    async with httpx.AsyncClient(timeout=20) as cli:
        r = await cli.get(url, headers=headers, params=params)
    if r.status_code == 401:
        raise ExpiredEA("EA session expired")
    if r.status_code == 429:
        retry_after = r.headers.get("Retry-After", "30")
        try: retry_after = int(retry_after)
        except: retry_after = 30
        raise RateLimitedEA(retry_after)
    r.raise_for_status()
    return r.json()

async def ea_lowest_bin_price(card_id: int, sid: str) -> Optional[int]:
    """Lowest active buyNowPrice for a card from EA's transfer market, or None if no live listings."""
    data = await ea_get(
        "transfermarket",
        sid,
        params={"start": 0, "num": 21, "type": "player", "maskedDefId": card_id},
    )
    prices = [
        a["buyNowPrice"]
        for a in (data.get("auctionInfo") or [])
        if a.get("tradeState") == "active" and isinstance(a.get("buyNowPrice"), (int, float))
    ]
    return min(prices) if prices else None
