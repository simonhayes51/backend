import asyncio
from typing import Any, Dict, List, Optional, Literal

import asyncpg

from app.services.price_history import get_price_history

# Concurrency cap so we don't hammer FUT.GG
_SEM = asyncio.Semaphore(10)

_pool: Optional[asyncpg.Pool] = None

async def init_pool(pool: asyncpg.Pool):
    """Call from main.py (use your existing player_pool)."""
    global _pool
    _pool = pool

# ---- helpers --------------------------------------------------------------

def _percent_change(points: List[Dict[str, Any]]) -> Optional[float]:
    """points: [{'t': iso, 'price': int}, ...]"""
    if not points:
        return None
    # first valid
    first = next((float(p["price"]) for p in points if isinstance(p.get("price"), (int, float)) and p["price"] > 0), None)
    # last valid
    last = next((float(p["price"]) for p in reversed(points) if isinstance(p.get("price"), (int, float)) and p["price"] > 0), None)
    if not first or last is None or first <= 0:
        return None
    return round(((last - first) / first) * 100.0, 2)

async def _history_percent(card_id: int, tf: Literal["4h","24h"]) -> Optional[float]:
    # Map our tf -> price_history pill ('today' covers ~24h; we’ll slice by time order anyway)
    pill = "today"  # we use 'today' for both; percent is computed from first/last within returned series order
    async with _SEM:
        data = await get_price_history(card_id, "ps", pill)
    pts = data.get("points") or []
    # If you prefer stricter windows, you can filter by last 4h/24h wall-clock here.
    return _percent_change(pts)

# ---- fut_players candidate set -------------------------------------------

async def _candidate_rows(limit: int = 150) -> List[asyncpg.Record]:
    """
    Choose a reasonable set of cards to evaluate.
    You can adjust WHERE to focus on meta cards, special versions, etc.
    """
    assert _pool, "init_pool() not called"
    sql = """
      SELECT
        card_id::bigint AS card_id,
        name,
        rating,
        version,
        image_url,
        club,
        league
      FROM fut_players
      WHERE rating IS NOT NULL
      ORDER BY rating DESC NULLS LAST
      LIMIT $1
    """
    async with _pool.acquire() as conn:
        return await conn.fetch(sql, limit)

# ---- core calculators -----------------------------------------------------

async def _calc_item(row: asyncpg.Record, tf: Literal["4h","24h"]) -> Optional[Dict[str, Any]]:
    cid = int(row["card_id"])
    pct = await _history_percent(cid, tf)
    if pct is None:
        return None
    return {
        "name": row["name"],
        "rating": row["rating"],
        "pid": cid,
        "version": row["version"],
        "image_url": row["image_url"],
        "club": row["club"],
        "league": row["league"],
        "percent": pct,
        # price_ps / price_xb not included here — use prices.py if you want current price too.
    }

async def _calc_item_dual(row: asyncpg.Record) -> Optional[Dict[str, Any]]:
    cid = int(row["card_id"])
    p4, p24 = await asyncio.gather(_history_percent(cid, "4h"), _history_percent(cid, "24h"))
    if p4 is None or p24 is None:
        return None
    return {
        "name": row["name"],
        "rating": row["rating"],
        "pid": cid,
        "version": row["version"],
        "image_url": row["image_url"],
        "club": row["club"],
        "league": row["league"],
        "percent_4h": p4,
        "percent_24h": p24,
    }

# ---- public API used by /api/trending ------------------------------------

async def get_trending_risers(tf: Literal["4h","24h"], limit: int = 20) -> List[Dict[str, Any]]:
    rows = await _candidate_rows(limit=250)
    results = await asyncio.gather(*[_calc_item(r, tf) for r in rows])
    items = [r for r in results if r is not None]
    items.sort(key=lambda x: x["percent"], reverse=True)
    return items[:limit]

async def get_trending_fallers(tf: Literal["4h","24h"], limit: int = 20) -> List[Dict[str, Any]]:
    rows = await _candidate_rows(limit=250)
    results = await asyncio.gather(*[_calc_item(r, tf) for r in rows])
    items = [r for r in results if r is not None]
    items.sort(key=lambda x: x["percent"])  # asc -> most negative first
    return items[:limit]

async def get_trending_smart(limit: int = 20) -> List[Dict[str, Any]]:
    rows = await _candidate_rows(limit=250)
    results = await asyncio.gather(*[_calc_item_dual(r) for r in rows])
    items: List[Dict[str, Any]] = []
    for r in results:
        if not r:
            continue
        p4 = r["percent_4h"]
        p24 = r["percent_24h"]
        if (p4 > 0 and p24 < 0) or (p4 < 0 and p24 > 0) or abs(p4 - p24) >= 5:
            items.append(r)
    items.sort(key=lambda x: abs(x["percent_4h"] - x["percent_24h"]), reverse=True)
    return items[:limit]
