# app/services/player_card_generation.py
#
# ensure_generated_player_card() is the one entry point everything else
# (admin API route, bulk script) calls. It owns: deciding whether a
# regeneration is actually needed (render-hash comparison), the
# generating->ready/error status lifecycle, and never discarding a
# previously-valid image just because a later regeneration failed.
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import asyncpg
from playwright.async_api import Browser

from app.services.object_storage import upload_png
from app.services.player_card_data import fetch_player_render_data
from app.services.player_card_hash import compute_card_render_hash
from app.services.player_card_render import (
    EXPORT_HEIGHT,
    EXPORT_WIDTH,
    PlayerCardRenderError,
    render_player_card_png,
)

logger = logging.getLogger("player_card_generation")

# A 'generating' row older than this is treated as abandoned (crashed
# worker, killed request) rather than a real in-flight job, so it doesn't
# permanently wedge that card out of ever being regenerated again.
_STALE_GENERATING_AFTER_SECONDS = 5 * 60

# One lock per card_id, scoped to this process - enough to stop N
# concurrent admin clicks (or a bulk run overlapping an admin click) on the
# *same* card from launching N simultaneous Chromium instances for it.
# Cross-process de-duplication isn't attempted (no queue/advisory lock)
# because generation is admin-triggered/bulk-script-triggered, not a public
# hot path where cross-process races are expected in practice.
_locks: Dict[str, asyncio.Lock] = {}


def _lock_for(card_id: str) -> asyncio.Lock:
    lock = _locks.get(card_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[card_id] = lock
    return lock


class PlayerCardNotFoundError(RuntimeError):
    pass


def _storage_key(card_id: str, render_hash: str) -> str:
    return f"fc26/generated-player-cards/{card_id}/{render_hash[:16]}.png"


def _public_result(row: Dict[str, Any], generated: bool) -> Dict[str, Any]:
    return {
        "ok": True,
        "generated": generated,
        "imageUrl": row.get("generated_card_url"),
        "hash": row.get("generated_card_hash"),
        "width": row.get("generated_card_width"),
        "height": row.get("generated_card_height"),
        "status": row.get("generated_card_status"),
        "error": row.get("generated_card_error"),
        "generatedAt": row["generated_card_at"].isoformat() if row.get("generated_card_at") else None,
    }


async def _mark_status(
    conn: asyncpg.Connection, card_id: str, status: str, error: Optional[str] = None
) -> None:
    await conn.execute(
        """
        UPDATE fut_players
           SET generated_card_status = $2,
               generated_card_error = $3
         WHERE card_id::text = $1
        """,
        str(card_id), status, error,
    )


async def ensure_generated_player_card(
    pool: asyncpg.Pool,
    card_id: str,
    force: bool = False,
    browser: Optional[Browser] = None,
) -> Dict[str, Any]:
    async with _lock_for(str(card_id)):
        async with pool.acquire() as conn:
            player_row = await fetch_player_render_data(conn, card_id)
            if player_row is None:
                raise PlayerCardNotFoundError(f"No fut_players row for card_id={card_id}")

            current_hash = compute_card_render_hash(player_row)

            is_generating = player_row.get("generated_card_status") == "generating"
            generating_is_stale = True
            if is_generating and player_row.get("generated_card_at"):
                age = (datetime.now(timezone.utc) - player_row["generated_card_at"]).total_seconds()
                generating_is_stale = age > _STALE_GENERATING_AFTER_SECONDS

            if not force and is_generating and not generating_is_stale:
                # Another request is already generating this exact card
                # right now (different process, so the in-process lock
                # above didn't catch it) - don't pile on a second Chromium
                # launch, just report the in-progress state honestly.
                return _public_result(player_row, generated=False)

            up_to_date = (
                not force
                and player_row.get("generated_card_status") == "ready"
                and player_row.get("generated_card_url")
                and player_row.get("generated_card_hash") == current_hash
            )
            if up_to_date:
                return _public_result(player_row, generated=False)

            await _mark_status(conn, card_id, "generating")

        try:
            rendered = await render_player_card_png(card_id, browser=browser)
        except PlayerCardRenderError as exc:
            logger.error("Card %s render failed: %s", card_id, exc)
            async with pool.acquire() as conn:
                await _mark_status(conn, card_id, "error", str(exc)[:2000])
                row = await fetch_player_render_data(conn, card_id)
            return _public_result(row, generated=False)
        except Exception as exc:  # unexpected - still record, still don't lose the old URL
            logger.exception("Card %s render raised an unexpected error", card_id)
            async with pool.acquire() as conn:
                await _mark_status(conn, card_id, "error", f"Unexpected error: {exc}"[:2000])
                row = await fetch_player_render_data(conn, card_id)
            return _public_result(row, generated=False)

        if rendered.width <= 0 or rendered.height <= 0:
            async with pool.acquire() as conn:
                await _mark_status(conn, card_id, "error", "Rendered PNG had invalid dimensions")
                row = await fetch_player_render_data(conn, card_id)
            return _public_result(row, generated=False)

        key = _storage_key(str(card_id), current_hash)
        try:
            image_url = await upload_png(key, rendered.png_bytes)
        except Exception as exc:
            logger.error("Card %s upload failed: %s", card_id, exc)
            async with pool.acquire() as conn:
                await _mark_status(conn, card_id, "error", f"Upload failed: {exc}"[:2000])
                row = await fetch_player_render_data(conn, card_id)
            return _public_result(row, generated=False)

        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    UPDATE fut_players
                       SET generated_card_url = $2,
                           generated_card_key = $3,
                           generated_card_hash = $4,
                           generated_card_width = $5,
                           generated_card_height = $6,
                           generated_card_at = NOW(),
                           generated_card_status = 'ready',
                           generated_card_error = NULL
                     WHERE card_id::text = $1
                    RETURNING generated_card_url, generated_card_key, generated_card_hash,
                              generated_card_width, generated_card_height, generated_card_at,
                              generated_card_status, generated_card_error
                    """,
                    str(card_id), image_url, key, current_hash, rendered.width, rendered.height,
                )
        except Exception:
            # The object landed in the bucket but the DB write failed - log
            # everything needed to find and either retry-link or garbage
            # collect the orphaned object by hand; never invent a fake
            # success response.
            logger.error(
                "Card %s: PNG uploaded to key=%s hash=%s but the DB update failed - orphaned object",
                card_id, key, current_hash,
            )
            raise

        return _public_result(dict(row), generated=True)


__all__ = [
    "ensure_generated_player_card",
    "PlayerCardNotFoundError",
    "EXPORT_WIDTH",
    "EXPORT_HEIGHT",
]
