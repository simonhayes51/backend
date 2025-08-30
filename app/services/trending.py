import os
import asyncio
import asyncpg
from typing import Any, Dict, List, Optional, Literal

# Reuse your existing price history utility
from app.services.price_history import get_price_history

PLAYER_DATABASE_URL = os.getenv("PLAYER_DATABASE_URL") or os.getenv("DATABASE_URL")
if not PLAYER_DATABASE_URL:
    raise RuntimeError("PLAYER_DATABASE_URL or DATABASE_URL must be set")

_pool: Optional[asyncpg.Pool] = None
_SEM = asyncio.Semaphore(8)  # limit concurrency so we don't hammer upstream

async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(PLAYER_DATABASE_URL, min_size=1, max_size=10)
    return _pool

async def _latest_price_from_history(player_id: int, platform: str, tf: Literal["4h","24h"]) -> Optional[int]:
    """Return the most recent valid price from history for a platform/timeframe."""
    try:
        data = await get_price_history(player_id, platform, tf)
        pts = data.get("points") or []
        for p in reversed(pts):
            price = p.get("price")
            if isinstance(price, (int, float)) and price > 0:
                return int(price)
    except Exception:
        pass
    return None

def _percent_change(points: List[Dict[str, Any]]) -> Optional[float]:
    """Compute percent change from first valid to last valid price in the series."""
    if not points:
        return None
    # first valid
    first = None
    for p in points:
        v = p.get("price")
        if isinstance(v, (int, float)) and v > 0:
            first = float(v)
            break
    if not first or first <= 0:
        return None
    # last valid
    last = None
    for p in reversed(points):
        v = p.get("price")
        if isinstance(v, (int, float)) and v > 0:
            last = float(v)
            break
    if last is None:
        return None
    return round(((last - first) / first) * 100.0, 2)

async def _compute_for_player(row: asyncpg.Record, tf: Literal["4h","24h"]) -> Optional[Dict[str, Any]]:
    """
    Build the unified payload for one player for a single timeframe.
    Uses PS/Xbox latest prices and computes percent (PS based).
    """
    card_id = int(row["card_id"])
    name = row["name"]
    rating = row["rating"]
    version = row["version"]
    image_url = row["image_url"]
    club = row["club"]
    league = row["league"]

    async with _SEM:
        try:
            # Use PS series to compute percentage
            ps_hist = await get_price_history(card_id, "ps", tf)
            percent = _percent_change(ps_hist.get("points") or [])
            # latest prices
            price_ps = await _latest_price_from_history(card_id, "ps", tf)
            price_xb = await _latest_price_from_history(card_id, "xbox", tf)
        except Exception:
            return None

    if percent is None and price_ps is None and price_xb is None:
        return None

    return {
        "name": name,
        "rating": rating,
        "pid": card_id,
        "version": version,
        "image_url": image_url,
        "club": club,
        "league": league,
        "price_ps": price_ps,
        "price_xb": price_xb,
        "percent": percent,
    }

async def _compute_for_player_dual(row: asyncpg.Record) -> Optional[Dict[str, Any]]:
    """
    For Smart Movers: compute 4h and 24h in one shot.
    """
    card_id = int(row["card_id"])
    name = row["name"]
    rating = row["rating"]
    version = row["version"]
    image_url = row["image_url"]

    async with _SEM:
        try:
            ps4 = await get_price_history(card_id, "ps", "4h")
            ps24 = await get_price_history(card_id, "ps", "24h")
            price_ps = await _latest_price_from_history(card_id, "ps", "24h")  # show a stable latest
            price_xb = await _latest_price_from_history(card_id, "xbox", "24h")
        except Exception:
            return None

    p4 = _percent_change(ps4.get("points") or [])
    p24 = _percent_change(ps24.get("points") or [])

    if p4 is None and p24 is None:
        return None

    return {
        "name": name,
        "rating": rating,
        "pid": card_id,
        "version": version,
        "image_url": image_url,
        "price_ps": price_ps,
        "price_xb": price_xb,
        "percent_4h": p4,
        "percent_24h": p24,
    }

async def _candidate_rows(limit: int = 120) -> List[asyncpg.Record]:
    """
    Pull a reasonable set of candidates from fut_players to evaluate.
    Prefer higher-rated and those with a known current price.
    """
    pool = await _get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT card_id, name, rating, version, image_url, club, league
            FROM fut_players
            WHERE price IS NOT NULL
            ORDER BY rating DESC NULLS LAST, price DESC NULLS LAST
            LIMIT $1
            """,
            limit,
        )

async def get_trending_risers(tf: Literal["4h","24h"]) -> List[Dict[str, Any]]:
    rows = await _candidate_rows()
    results = await asyncio.gather(*[_compute_for_player(r, tf) for r in rows])
    items = [r for r in results if r and isinstance(r.get("percent"), (int, float))]
    items.sort(key=lambda x: x["percent"], reverse=True)
    return items[:20]

async def get_trending_fallers(tf: Literal["4h","24h"]) -> List[Dict[str, Any]]:
    rows = await _candidate_rows()
    results = await asyncio.gather(*[_compute_for_player(r, tf) for r in rows])
    items = [r for r in results if r and isinstance(r.get("percent"), (int, float))]
    items.sort(key=lambda x: x["percent"])  # ascending -> most negative first
    return items[:20]

async def get_trending_smart() -> List[Dict[str, Any]]:
    rows = await _candidate_rows()
    results = await asyncio.gather(*[_compute_for_player_dual(r) for r in rows])
    items = []
    for r in results:
        if not r:
            continue
        p4 = r.get("percent_4h")
        p24 = r.get("percent_24h")
        # "Smart" = moving in opposite directions or large divergence
        if isinstance(p4, (int, float)) and isinstance(p24, (int, float)):
            if (p4 > 0 and p24 < 0) or (p4 < 0 and p24 > 0) or abs(p4 - p24) >= 5:
                items.append(r)
    # sort by magnitude of divergence
    items.sort(key=lambda x: abs((x.get("percent_4h") or 0) - (x.get("percent_24h") or 0)), reverse=True)
    return items[:20]
