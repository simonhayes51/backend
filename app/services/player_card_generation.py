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
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import asyncpg
from playwright.async_api import Browser, Page, async_playwright

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

_STALE_GENERATING_AFTER_SECONDS = 5 * 60
_locks: Dict[str, asyncio.Lock] = {}


def _lock_for(card_id: str) -> asyncio.Lock:
    lock = _locks.get(card_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[card_id] = lock
    return lock


# Shared, lazily-launched Chromium instance for callers that don't pass their
# own browser/page (the admin single-card route and, primarily, the new
# on-demand path in app/services/player_card_ondemand.py). Cold-launching
# Chromium per call was costing ~1-2s of pure browser-startup overhead on
# every cache-miss render; this launches once per process and is reused for
# every subsequent context/page. Same launch args as the batch script
# (scripts/generate_player_cards.py) and the backfill worker
# (player_card_backfill.py) so behavior is identical, just not re-launched.
_shared_browser: Optional[Browser] = None
_shared_browser_lock = asyncio.Lock()

_LAUNCH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]


async def _get_shared_browser() -> Browser:
    global _shared_browser
    async with _shared_browser_lock:
        if _shared_browser is not None and _shared_browser.is_connected():
            return _shared_browser
        if _shared_browser is not None:
            logger.warning("shared player-card browser was disconnected; relaunching")
        pw = await async_playwright().start()
        _shared_browser = await pw.chromium.launch(headless=True, args=_LAUNCH_ARGS)
        return _shared_browser


async def close_shared_browser() -> None:
    """Best-effort cleanup for app shutdown. Safe to call even if the shared
    browser was never launched."""
    global _shared_browser
    async with _shared_browser_lock:
        if _shared_browser is not None:
            try:
                await _shared_browser.close()
            except Exception:
                logger.exception("error closing shared player-card browser")
            _shared_browser = None


class PlayerCardNotFoundError(RuntimeError):
    pass


def _storage_key(card_id: str, render_hash: str) -> str:
    """Return an immutable, unique object key for every actual render.

    R2 objects are served with a one-year immutable cache header. Reusing a key
    during force regeneration therefore leaves browsers/CDNs showing the old
    pixels. A short random revision makes every successful render a new URL
    while retaining the card id and render hash for traceability.
    """
    revision = secrets.token_hex(6)
    return f"fc26/generated-player-cards/{card_id}/{render_hash[:16]}-{revision}.png"


def _versioned_url(url: Optional[str], generated_at: Optional[datetime]) -> Optional[str]:
    """Cache-bust legacy rows whose object key was overwritten in place."""
    if not url or not generated_at:
        return url
    separator = "&" if "?" in url else "?"
    version = int(generated_at.timestamp() * 1000)
    return f"{url}{separator}v={version}"


def _public_result(row: Dict[str, Any], generated: bool) -> Dict[str, Any]:
    generated_at = row.get("generated_card_at")
    return {
        "ok": True,
        "generated": generated,
        "imageUrl": _versioned_url(row.get("generated_card_url"), generated_at),
        "hash": row.get("generated_card_hash"),
        "width": row.get("generated_card_width"),
        "height": row.get("generated_card_height"),
        "status": row.get("generated_card_status"),
        "error": row.get("generated_card_error"),
        "generatedAt": generated_at.isoformat() if generated_at else None,
        "flagged": bool(row.get("generated_card_flagged")),
        "flagReason": row.get("generated_card_flag_reason"),
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
    page: Optional[Page] = None,
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
            effective_browser = browser
            if effective_browser is None and page is None:
                # Callers that pass neither (the on-demand path, and the
                # admin single-card generate route) reuse one shared,
                # already-warm Chromium instance instead of paying a fresh
                # launch on every cache miss.
                effective_browser = await _get_shared_browser()
            rendered = await render_player_card_png(card_id, browser=effective_browser, page=page)
        except PlayerCardRenderError as exc:
            logger.error("Card %s render failed: %s", card_id, exc)
            async with pool.acquire() as conn:
                await _mark_status(conn, card_id, "error", str(exc)[:2000])
                row = await fetch_player_render_data(conn, card_id)
            return _public_result(row, generated=False)
        except Exception as exc:
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
                           generated_card_error = NULL,
                           generated_card_flagged = FALSE,
                           generated_card_flag_reason = NULL
                     WHERE card_id::text = $1
                    RETURNING generated_card_url, generated_card_key, generated_card_hash,
                              generated_card_width, generated_card_height, generated_card_at,
                              generated_card_status, generated_card_error,
                              generated_card_flagged, generated_card_flag_reason
                    """,
                    str(card_id), image_url, key, current_hash, rendered.width, rendered.height,
                )
        except Exception:
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
    "close_shared_browser",
]
