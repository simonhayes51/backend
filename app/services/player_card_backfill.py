# app/services/player_card_backfill.py
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
from app.services.player_card_render import (
    EXPORT_DEVICE_SCALE,
    EXPORT_HEIGHT,
    EXPORT_WIDTH,
)

logger = logging.getLogger("player_card_backfill")

VALID_MODES = ("missing", "stale")
VALID_CARD_GROUPS = ("all", "special", "gold", "silver", "bronze")
DEFAULT_MAX_CARDS = 50_000

# A special card normally has card_cutout_type='special'. Older imported rows
# can have that field blank, so the p*.png cutout filename is used as a safe
# fallback. Base cards use a numeric cutout filename.
_SPECIAL_SQL = """(
    LOWER(COALESCE(card_cutout_type, '')) = 'special'
    OR COALESCE(card_cutout_image, '') ~* '/p[^/]*\\.png(?:\\?.*)?$'
)"""


def _group_sql(card_group: str) -> str:
    if card_group == "special":
        return _SPECIAL_SQL
    if card_group == "gold":
        return f"NOT {_SPECIAL_SQL} AND COALESCE(rating, 0) >= 75"
    if card_group == "silver":
        return f"NOT {_SPECIAL_SQL} AND COALESCE(rating, 0) BETWEEN 65 AND 74"
    if card_group == "bronze":
        return f"NOT {_SPECIAL_SQL} AND COALESCE(rating, 0) < 65"
    return "TRUE"


async def candidate_card_ids(
    pool: asyncpg.Pool,
    mode: str,
    limit: int = DEFAULT_MAX_CARDS,
    player_id: Optional[str] = None,
    card_group: str = "all",
) -> List[str]:
    """Return the filtered work list for this run, up to the safety cap."""
    if player_id:
        return [player_id]

    group_filter = _group_sql(card_group)
    if mode == "stale":
        eligibility = "generated_card_status = 'ready' AND generated_card_hash IS NOT NULL"
        order_by = "generated_card_at ASC NULLS FIRST"
    else:
        eligibility = "(generated_card_url IS NULL OR generated_card_status = 'error')"
        order_by = "card_id ASC"

    query = f"""
        SELECT card_id
        FROM fut_players
        WHERE {eligibility}
          AND ({group_filter})
        ORDER BY {order_by}
        LIMIT $1
    """

    async with pool.acquire() as conn:
        rows = await conn.fetch(query, limit)
    return [str(r["card_id"]) for r in rows]


async def is_actually_stale(pool: asyncpg.Pool, card_id: str) -> bool:
    async with pool.acquire() as conn:
        row = await fetch_player_render_data(conn, card_id)
    if row is None:
        return False
    return compute_card_render_hash(row) != row.get("generated_card_hash")


async def _filter_stale(pool: asyncpg.Pool, card_ids: List[str]) -> List[str]:
    semaphore = asyncio.Semaphore(20)

    async def check(card_id: str) -> Optional[str]:
        async with semaphore:
            return card_id if await is_actually_stale(pool, card_id) else None

    checked = await asyncio.gather(*(check(card_id) for card_id in card_ids))
    return [card_id for card_id in checked if card_id]


_state: Dict[str, Any] = {"running": False}


def get_backfill_status() -> Dict[str, Any]:
    return dict(_state)


def _update_rate() -> None:
    elapsed = max(0.001, time.time() - float(_state.get("started_at") or time.time()))
    processed = int(_state.get("processed") or 0)
    total = int(_state.get("total") or 0)
    rate_per_minute = processed / elapsed * 60
    remaining = max(0, total - processed)
    _state["rate_per_minute"] = round(rate_per_minute, 1)
    _state["remaining"] = remaining
    _state["eta_seconds"] = round(remaining / (rate_per_minute / 60)) if rate_per_minute > 0 else None


async def start_backfill(
    pool: asyncpg.Pool,
    mode: str = "missing",
    limit: int = DEFAULT_MAX_CARDS,
    concurrency: int = 3,
    force: bool = False,
    card_group: str = "all",
) -> Dict[str, Any]:
    if _state.get("running"):
        return {"ok": False, "already_running": True, **_state}

    _state.clear()
    _state.update(
        {
            "running": True,
            "phase": "selecting",
            "mode": mode,
            "card_group": card_group,
            "limit": limit,
            "concurrency": concurrency,
            "force": force,
            "total": 0,
            "processed": 0,
            "succeeded": 0,
            "failed": 0,
            "remaining": 0,
            "rate_per_minute": 0,
            "eta_seconds": None,
            "started_at": time.time(),
            "finished_at": None,
            "last_error": None,
        }
    )

    card_ids = await candidate_card_ids(pool, mode, limit, card_group=card_group)
    if mode == "stale":
        _state["phase"] = "checking_stale"
        card_ids = await _filter_stale(pool, card_ids)

    _state["phase"] = "rendering"
    _state["total"] = len(card_ids)
    _state["remaining"] = len(card_ids)

    if not card_ids:
        _state["running"] = False
        _state["phase"] = "finished"
        _state["finished_at"] = time.time()
        return {"ok": True, "already_running": False, **_state}

    asyncio.create_task(_run(pool, card_ids, concurrency, force))
    return {"ok": True, "already_running": False, **_state}


async def _run(pool: asyncpg.Pool, card_ids: List[str], concurrency: int, force: bool) -> None:
    queue: asyncio.Queue[str] = asyncio.Queue()
    for card_id in card_ids:
        queue.put_nowait(card_id)

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            )

            async def worker(worker_number: int) -> None:
                context = await browser.new_context(
                    viewport={"width": EXPORT_WIDTH, "height": EXPORT_HEIGHT},
                    device_scale_factor=EXPORT_DEVICE_SCALE,
                )
                page = await context.new_page()
                try:
                    while True:
                        try:
                            card_id = queue.get_nowait()
                        except asyncio.QueueEmpty:
                            return

                        ok = False
                        try:
                            result = await ensure_generated_player_card(
                                pool,
                                card_id,
                                force=force,
                                page=page,
                            )
                            ok = result.get("status") != "error"
                        except PlayerCardNotFoundError:
                            logger.warning("backfill: card_id=%s not found, skipping", card_id)
                        except Exception:
                            logger.exception(
                                "backfill worker=%s card_id=%s unexpected failure",
                                worker_number,
                                card_id,
                            )
                        finally:
                            queue.task_done()

                        _state["processed"] = int(_state.get("processed") or 0) + 1
                        if ok:
                            _state["succeeded"] = int(_state.get("succeeded") or 0) + 1
                        else:
                            _state["failed"] = int(_state.get("failed") or 0) + 1
                        _update_rate()
                finally:
                    await context.close()

            try:
                workers = [
                    asyncio.create_task(worker(number + 1))
                    for number in range(max(1, concurrency))
                ]
                await asyncio.gather(*workers)
            finally:
                await browser.close()
    except Exception as exc:
        logger.exception("backfill job crashed")
        _state["last_error"] = str(exc)[:500]
    finally:
        _update_rate()
        _state["running"] = False
        _state["phase"] = "finished"
        _state["finished_at"] = time.time()
