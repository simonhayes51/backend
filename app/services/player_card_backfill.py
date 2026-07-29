# app/services/player_card_backfill.py
#
# Shared candidate-selection logic for "generate every missing/stale card"
# (used by both the CLI script scripts/generate_player_cards.py and the
# admin-triggered in-process job below), plus the job runner itself.
#
# The admin API route can't just call the CLI script as a subprocess (the
# deploy environment has no shell access - see app/routers/admin.py's own
# docstring on exactly this constraint), so this runs as a plain asyncio
# background task inside the running web process instead: one global job
# at a time, progress tracked in a module-level dict the status endpoint
# polls. That's a real limitation (a Railway restart mid-run loses
# progress state, and there's no cross-instance coordination if the web
# service ever scales beyond one replica) - acceptable here because this
# is an occasional, admin-operated action, not a hot path, and reruns are
# idempotent (already-generated cards are simply skipped next time).
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

import asyncpg
from playwright.async_api import async_playwright

from app.services.player_card_data import fetch_player_render_data
from app.services.player_card_generation import (
    PlayerCardNotFoundError,
    ensure_generated_player_card,
)
from app.services.player_card_hash import compute_card_render_hash

logger = logging.getLogger("player_card_backfill")

VALID_MODES = ("missing", "stale")


async def candidate_card_ids(
    pool: asyncpg.Pool, mode: str, limit: int, player_id: Optional[str] = None
) -> List[str]:
    if player_id:
        return [player_id]

    async with pool.acquire() as conn:
        if mode == "stale":
            rows = await conn.fetch(
                """
                SELECT card_id FROM fut_players
                WHERE generated_card_status = 'ready' AND generated_card_hash IS NOT NULL
                ORDER BY generated_card_at ASC NULLS FIRST
                LIMIT $1
                """,
                limit,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT card_id FROM fut_players
                WHERE generated_card_url IS NULL OR generated_card_status = 'error'
                ORDER BY card_id ASC
                LIMIT $1
                """,
                limit,
            )
    return [str(r["card_id"]) for r in rows]


async def is_actually_stale(pool: asyncpg.Pool, card_id: str) -> bool:
    async with pool.acquire() as conn:
        row = await fetch_player_render_data(conn, card_id)
    if row is None:
        return False
    return compute_card_render_hash(row) != row.get("generated_card_hash")


# ---------------------------------------------------------------------------
# In-process job runner for the admin-triggered "Backfill" button.
# ---------------------------------------------------------------------------

_state: Dict[str, Any] = {"running": False}


def get_backfill_status() -> Dict[str, Any]:
    return dict(_state)


async def start_backfill(
    pool: asyncpg.Pool,
    mode: str = "missing",
    limit: int = 200,
    concurrency: int = 1,
    force: bool = False,
) -> Dict[str, Any]:
    if _state.get("running"):
        return {"ok": False, "already_running": True, **_state}

    card_ids = await candidate_card_ids(pool, mode, limit)
    if mode == "stale":
        card_ids = [cid for cid in card_ids if await is_actually_stale(pool, cid)]

    _state.clear()
    _state.update(
        {
            "running": True,
            "mode": mode,
            "limit": limit,
            "concurrency": concurrency,
            "force": force,
            "total": len(card_ids),
            "processed": 0,
            "succeeded": 0,
            "failed": 0,
            "started_at": time.time(),
            "finished_at": None,
            "last_error": None,
        }
    )

    if not card_ids:
        _state["running"] = False
        _state["finished_at"] = time.time()
        return {"ok": True, "already_running": False, **_state}

    asyncio.create_task(_run(pool, card_ids, concurrency, force))
    return {"ok": True, "already_running": False, **_state}


async def _run(pool: asyncpg.Pool, card_ids: List[str], concurrency: int, force: bool) -> None:
    semaphore = asyncio.Semaphore(max(1, concurrency))

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            )

            async def _one(card_id: str) -> None:
                ok = False
                try:
                    result = await ensure_generated_player_card(pool, card_id, force=force, browser=browser)
                    ok = result.get("status") != "error"
                except PlayerCardNotFoundError:
                    logger.warning("backfill: card_id=%s not found, skipping", card_id)
                except Exception:
                    logger.exception("backfill: card_id=%s unexpected failure", card_id)

                _state["processed"] = _state.get("processed", 0) + 1
                if ok:
                    _state["succeeded"] = _state.get("succeeded", 0) + 1
                else:
                    _state["failed"] = _state.get("failed", 0) + 1

            async def _bounded(card_id: str) -> None:
                async with semaphore:
                    await _one(card_id)

            try:
                await asyncio.gather(*(_bounded(cid) for cid in card_ids))
            finally:
                await browser.close()
    except Exception as exc:
        logger.exception("backfill job crashed")
        _state["last_error"] = str(exc)[:500]
    finally:
        _state["running"] = False
        _state["finished_at"] = time.time()
