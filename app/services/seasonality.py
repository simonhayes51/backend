# app/services/seasonality.py
from __future__ import annotations
from datetime import datetime, timezone
from typing import Dict, List, Tuple
import statistics as st
import asyncpg

from app.services.price_history import get_price_history

async def seasonal_profile(pool: asyncpg.Pool, player_id: int, platform: str, days: int = 14) -> Dict[str, float]:
    hist = await get_price_history(player_id, pill=f"{days}d", platform=platform)
    if not hist or len(hist) < 12:
        return {}
    # bucket by (dow, hour)
    buckets: Dict[Tuple[int,int], List[int]] = {}
    all_prices: List[int] = []
    for p in hist:
        price = int(p.get("price") or 0)
        if price <= 0: 
            continue
        t = datetime.fromtimestamp(p["t"]/1000, tz=timezone.utc)
        buckets.setdefault((t.weekday(), t.hour), []).append(price)
        all_prices.append(price)
    if not all_prices:
        return {}
    gmed = st.median(all_prices)
    prof: Dict[str, float] = {}
    for (dow, hr), xs in buckets.items():
        xs = [x for x in xs if x > 0]
        if len(xs) < 3:
            continue
        med = st.median(xs)
        rel = (med - gmed) / gmed * 100.0
        prof[f"{dow}:{hr}"] = rel
    return prof

async def rewards_bias(pool: asyncpg.Pool, when: datetime) -> float:
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM market_windows WHERE active=TRUE")
    dow = when.weekday(); hr = when.hour
    bias = 0.0
    for r in rows:
        if r["dow"] != dow:
            continue
        start = int(r["start_hour_utc"]); dur = int(r["duration_hours"]); w = float(r["weight"])
        if start <= hr < start + dur:
            bias += w
    return bias
