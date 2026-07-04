# app/services/prices.py
# Current BIN price for a card, used by several services (watchlist polling,
# deal confidence, trending, trade finder). Used to hit fut.gg's price API
# first with a 10s timeout on every single call - fut.gg 403s scrapers now,
# so every call paid that full dead-host latency before ever falling back.
# At the concurrency these callers run at (some scan 100+ candidates), that
# was minutes of pure timeout before anything could return - the likely
# cause of pages built on this (e.g. Trade Finder) appearing to never load.
#
# Now prefers fut_players.price_num (no network call at all - kept current
# by the futbin sync crawl), then a live futbin.com fetch, then the most
# recent point from bucketed sales history. Never touches fut.gg.

from __future__ import annotations

from typing import Optional

from app.db import get_player_pool
from app.futbin_client import fetch_price_by_card_id
from app.services.price_history import get_price_history


async def _db_price(card_id: int) -> int:
    try:
        pool = await get_player_pool()
        async with pool.acquire() as conn:
            val = await conn.fetchval(
                "SELECT price_num FROM fut_players WHERE card_id = $1", card_id
            )
        return int(val) if val else 0
    except Exception:
        return 0


async def _fallback_from_history(card_id: int, platform: str) -> int:
    try:
        hist = await get_price_history(card_id, platform, "today")
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
    DB price_num first (fast), then a live futbin fetch, then history.
    """
    price = await _db_price(card_id)
    if price > 0:
        return price

    try:
        live = await fetch_price_by_card_id(card_id, platform)
        if live:
            return int(live)
    except Exception:
        pass

    return await _fallback_from_history(card_id, platform)
