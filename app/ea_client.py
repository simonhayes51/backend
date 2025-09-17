import os
import httpx
from typing import Optional

EA_BASE = "https://utas.mob.v5.prd.futc-ext.gcp.ea.com/ut/game/fc26"

class ExpiredEA(Exception): pass
class RateLimitedEA(Exception):
    def __init__(self, retry_after: Optional[int]): self.retry_after = retry_after

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
