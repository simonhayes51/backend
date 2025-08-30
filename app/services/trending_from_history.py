# app/services/trending_from_history.py
# Trending built from FUT.GG history + fut_players metadata + current price (with fallback).
# Fixes:
# - Window slicing uses real UTC now (not last point)
# - 24h uses a wider pill ('3d') then slices to last 24h
# - Current price falls back to last history point if live is unavailable

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Literal
from datetime import datetime, timezone, timedelta

import asyncpg

from app.services.price_history import get_price_history
from app.services.prices import get_player_price  # now with stronger headers + fallback


_SEM = asyncio.Semaphore(10)
_pool: Optional[asyncpg.Pool] = None

async def init_pool(pool: asyncpg.Pool):
    global _pool
    _pool = pool

# ----------------- time helpers -----------------

def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)

def _iso_to_ms(iso_str: str) -> Optional[int]:
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except Exception:
        return None

def _slice_points_by_now(points: List[Dict[str, Any]], hours: int) -> List[Dict[str, Any]]:
    """Keep only points with timestamp >= (now - hours)."""
    if not points:
        return points
    start_ms = _now_ms() - hours * 3600 * 1000
    out: List[Dict[str, Any]] = []
    for p in points:
        ts = _iso_to_ms(p["t"])
        if ts is None:
            continue
        if ts >= start_ms:
            out.append(p)
    return out

def _percent_change(points: List[Dict[str, Any]]) -> Optional[float]:
    if not points:
        return None
    first = next((float(p["price"]) for p in points if isinstance(p.get("price"), (int, float)) and p["price"] > 0), None)
    last  = next((float(p["price"]) for p in reversed(points) if isinstance(p.get("price"), (int, float)) and p["price"] > 0), None)
    if not first or last is None or first <= 0:
        return None
    return round(((last - first) / first) * 100.0, 2)

async def _history_percent(card_id: int, tf: Literal["4h","24h"]) -> Optional[float]:
    pill = "today" if tf == "4h" else "3d"  # ensure we have >24h coverage
    async with _SEM:
        data = await get_price_history(card_id, "ps", pill)
    pts = data.get("points") or []
    if not pts:
        return None
    hours = 4 if tf == "4h" else 24
    pts = _slice_points_by_now(pts, hours)
    return _percent_change(pts)

# ----------------- fut_players candidates -----------------

async def _candidate_rows(limit: int = 300) -> List[asyncpg.Record]:
    """
    Adjust WHERE as desired. Requiring image_url ensures nice UI cards.
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
        AND image_url IS NOT NULL
      ORDER BY rating DESC NULLS LAST
      LIMIT $1
    """
    async with _pool.acquire() as conn:
        return await conn.fetch(sql, limit)

# ----------------- calculators -----------------

async def _calc_item(row: asyncpg.Record, tf: Literal["4h", "24h"]) -> Optional[Dict[str, Any]]:
    cid = int(row["card_id"])
    pct, ps_price = await asyncio.gather(
        _history_percent(cid, tf),
        get_player_price(cid, "ps"),
    )
    if pct is None:
        return None
    price_ps = ps_price if isinstance(ps_price, int) and ps_price > 0 else None
    return {
        "name": row["name"],
        "rating": row["rating"],
        "pid": cid,
        "version": row["version"],
        "image_url": row["image_url"],
        "club": row["club"],
        "league": row["league"],
        "percent": pct,
        "price_ps": price_ps,
        "price_xb": None,
    }

async def _calc_item_dual(row: asyncpg.Record) -> Optional[Dict[str, Any]]:
    cid = int(row["card_id"])
    (p4, p24), ps_price = await asyncio.gather(
        asyncio.gather(_history_percent(cid, "4h"), _history_percent(cid, "24h")),
        get_player_price(cid, "ps"),
    )
    if p4 is None or p24 is None:
        return None
    price_ps = ps_price if isinstance(ps_price, int) and ps_price > 0 else None
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
        "price_ps": price_ps,
        "price_xb": None,
    }

# ----------------- public API -----------------

async def get_trending_risers(tf: Literal["4h", "24h"], limit: int = 20) -> List[Dict[str, Any]]:
    rows = await _candidate_rows(limit=350)
    results = await asyncio.gather(*[_calc_item(r, tf) for r in rows])
    items = [r for r in results if r is not None]
    items.sort(key=lambda x: x["percent"], reverse=True)
    return items[:limit]

async def get_trending_fallers(tf: Literal["4h", "24h"], limit: int = 20) -> List[Dict[str, Any]]:
    rows = await _candidate_rows(limit=350)
    results = await asyncio.gather(*[_calc_item(r, tf) for r in rows])
    items = [r for r in results if r is not None]
    items.sort(key=lambda x: x["percent"])  # negative first
    return items[:limit]

async def get_trending_smart(limit: int = 20) -> List[Dict[str, Any]]:
    rows = await _candidate_rows(limit=350)
    results = await asyncio.gather(*[_calc_item_dual(r) for r in rows])
    items: List[Dict[str, Any]] = []
    for r in results:
        if not r:
            continue
        p4, p24 = r["percent_4h"], r["percent_24h"]
        if (p4 > 0 and p24 < 0) or (p4 < 0 and p24 > 0) or abs(p4 - p24) >= 5:
            items.append(r)
    items.sort(key=lambda x: abs(x["percent_4h"] - x["percent_24h"]), reverse=True)
    return items[:limit]