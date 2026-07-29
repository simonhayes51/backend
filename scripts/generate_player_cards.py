#!/usr/bin/env python
# scripts/generate_player_cards.py
#
# Controlled batch backfill for generated_card_url. Deliberately NOT a
# permanent Railway worker/cron (unlike scripts/refresh_all_prices_loop.py) -
# Chromium rendering is heavy relative to this app's usual aiohttp/asyncpg
# workloads, so this is meant to be run by hand (or from a one-off Railway
# job) with a low, explicit concurrency cap, not left running continuously.
#
# Usage:
#   python -m scripts.generate_player_cards --missing --limit=200 --concurrency=2
#   python -m scripts.generate_player_cards --stale --limit=500
#   python -m scripts.generate_player_cards --player-id=12345 --force
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

import asyncpg
from dotenv import load_dotenv
from playwright.async_api import async_playwright

from app.services.player_card_backfill import candidate_card_ids, is_actually_stale
from app.services.player_card_generation import (
    PlayerCardNotFoundError,
    ensure_generated_player_card,
)

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("generate_player_cards")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backfill/regenerate player-card PNGs")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--missing", action="store_true", help="cards with no generated_card_url or a prior error")
    mode.add_argument("--stale", action="store_true", help="cards whose stored hash no longer matches current data")
    mode.add_argument("--player-id", type=str, default=None, help="generate just this one card_id")
    p.add_argument("--limit", type=int, default=100, help="max cards to process this run")
    p.add_argument("--concurrency", type=int, default=1, help="parallel generations (Chromium is heavy - keep this low)")
    p.add_argument("--force", action="store_true", help="regenerate even if the stored hash already matches")
    return p.parse_args()


async def _run_one(pool, browser, card_id: str, force: bool) -> bool:
    try:
        result = await ensure_generated_player_card(pool, card_id, force=force, browser=browser)
    except PlayerCardNotFoundError:
        logger.warning("card_id=%s: not found, skipping", card_id)
        return False
    except Exception:
        logger.exception("card_id=%s: unexpected failure", card_id)
        return False

    if result.get("status") == "error":
        logger.error("card_id=%s: %s", card_id, result.get("error"))
        return False

    logger.info(
        "card_id=%s: %s (status=%s url=%s)",
        card_id, "generated" if result.get("generated") else "already current",
        result.get("status"), result.get("imageUrl"),
    )
    return True


async def main() -> int:
    args = _parse_args()
    dsn = os.getenv("PLAYER_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not dsn:
        logger.error("PLAYER_DATABASE_URL (or DATABASE_URL) is required")
        return 1

    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=max(2, args.concurrency))
    mode = "stale" if args.stale else "missing"
    card_ids = await candidate_card_ids(pool, mode, args.limit, player_id=args.player_id)

    if args.stale and not args.player_id:
        card_ids = [cid for cid in card_ids if await is_actually_stale(pool, cid)]

    total = len(card_ids)
    logger.info("Eligible cards this run: %d", total)
    if total == 0:
        await pool.close()
        return 0

    succeeded = 0
    failed = 0
    semaphore = asyncio.Semaphore(max(1, args.concurrency))

    async with async_playwright() as pw:
        # One Chromium process reused across the whole batch (each card
        # still gets its own browser context/page) rather than a fresh
        # launch per card - launching Chromium per player is by far the
        # slowest, most resource-hungry part of this pipeline at any real
        # batch size.
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )

        async def _bounded(cid: str) -> None:
            nonlocal succeeded, failed
            async with semaphore:
                ok = await _run_one(pool, browser, cid, args.force)
                if ok:
                    succeeded += 1
                else:
                    failed += 1

        try:
            await asyncio.gather(*(_bounded(cid) for cid in card_ids))
        finally:
            await browser.close()

    await pool.close()

    logger.info("Done. attempted=%d succeeded=%d failed=%d", total, succeeded, failed)
    # Only a total wipeout (nothing at all succeeded, despite eligible work
    # existing) counts as a meaningful overall failure worth a non-zero
    # exit - individual card failures are expected/logged and shouldn't
    # fail a whole CI/cron run.
    return 1 if succeeded == 0 and total > 0 else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
