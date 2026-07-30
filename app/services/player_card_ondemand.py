"""Trigger on-demand player card generation without blocking the request."""
from __future__ import annotations

import asyncio
import logging

import asyncpg

from app.services.player_card_generation import ensure_generated_player_card

logger = logging.getLogger(__name__)

_STALE_GENERATING_SECONDS = 300  # keep in sync with player_card_generation.py's own constant

# Cap concurrent on-demand Playwright renders per process so a burst of cold
# cache misses can't spawn unbounded Chromium instances alongside the batch script.
_ONDEMAND_SEMAPHORE = asyncio.Semaphore(6)

_CLAIM_SQL = """
    UPDATE fut_players
    SET generated_card_status = 'generating',
        generated_card_at = NOW()
    WHERE card_id::text = ANY($1::text[])
      AND (
        generated_card_status IS NULL
        OR generated_card_status = 'error'
        OR (generated_card_status = 'generating' AND generated_card_at < NOW() - INTERVAL '5 minutes')
        OR (generated_card_status = 'ready' AND generated_card_flagged = TRUE)
      )
    RETURNING card_id::text AS card_id
"""


async def _run_claimed(pool: asyncpg.Pool, card_id: str) -> None:
    async with _ONDEMAND_SEMAPHORE:
        try:
            await ensure_generated_player_card(pool, card_id)
        except Exception:
            logger.exception("on-demand card generation failed for card_id=%s", card_id)


async def ensure_cards_requested(pool: asyncpg.Pool, card_ids: list[str]) -> list[str]:
    """Atomically claim any of card_ids that need (re)generation and launch
    background generation tasks for them. Safe to call with duplicate/overlapping
    ids across concurrent requests and across app replicas -- the UPDATE's row
    locking is the sole source of truth for who "wins" a given card_id, so at
    most one Playwright render runs per card_id at a time process-wide-and-beyond.
    Returns the list of card_ids actually claimed (informational/logging only;
    callers should NOT wait on this to affect their response).
    """
    if not card_ids:
        return []
    unique_ids = list(dict.fromkeys(str(c) for c in card_ids))
    async with pool.acquire() as conn:
        rows = await conn.fetch(_CLAIM_SQL, unique_ids)
    claimed = [r["card_id"] for r in rows]
    for card_id in claimed:
        asyncio.create_task(_run_claimed(pool, card_id))
    return claimed


async def ensure_card_requested(pool: asyncpg.Pool, card_id: str) -> bool:
    claimed = await ensure_cards_requested(pool, [card_id])
    return card_id in claimed


__all__ = ["ensure_cards_requested", "ensure_card_requested"]
