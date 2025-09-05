# scripts/refresh_all_prices_loop.py
import os
import asyncio
import asyncpg
import logging
import random
import signal
import time
from typing import Optional, List, Tuple

# Uses your existing scraper
from app.services.prices import get_player_price  # (card_id: int, platform: str) -> Optional[int]

# -------------- Config --------------
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL env var is required")

PLATFORM = os.getenv("REFRESH_PLATFORM", "ps")          # ps | xbox | pc
CONCURRENCY = int(os.getenv("REFRESH_CONCURRENCY", "20"))
RETRIES = int(os.getenv("REFRESH_RETRIES", "2"))
BATCH_SIZE = int(os.getenv("REFRESH_BATCH_SIZE", "1000"))
INTERVAL_SECONDS = int(os.getenv("REFRESH_INTERVAL_SECONDS", str(30 * 60)))  # 30 minutes default

# -------------- Logging --------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("refresh_all_prices")

# -------------- Graceful shutdown --------------
_stop = asyncio.Event()
def _handle_stop(*_):
    log.info("Shutdown signal received.")
    _stop.set()

signal.signal(signal.SIGINT, _handle_stop)
signal.signal(signal.SIGTERM, _handle_stop)


async def _get_price_with_retries(card_id_int: int, platform: str) -> Optional[int]:
    """
    Try the upstream price function a few times with light jittered backoff.
    """
    last_err = None
    for attempt in range(1, RETRIES + 2):
        try:
            price = await get_player_price(card_id_int, platform)
            if isinstance(price, (int, float)):
                return int(price)
            return None
        except Exception as e:
            last_err = e
            await asyncio.sleep(0.35 * attempt + random.random() * 0.25)
    log.debug("card %s failed after retries: %s", card_id_int, last_err)
    return None


async def _fetch_all_card_ids(conn: asyncpg.Connection) -> List[str]:
    rows = await conn.fetch("SELECT card_id FROM fut_players")
    return [str(r["card_id"]) for r in rows if r["card_id"]]


async def _refresh_once(pool: asyncpg.Pool) -> Tuple[int, int, int]:
    """
    One full pass: pull all card_ids, fetch prices concurrently (bounded),
    update fut_players.price. Returns (total, updated, missing).
    """
    async with pool.acquire() as conn:
        # Ensure price column exists (safe no-op if already there)
        await conn.execute("ALTER TABLE fut_players ADD COLUMN IF NOT EXISTS price INTEGER")
        card_ids = await _fetch_all_card_ids(conn)

    total = len(card_ids)
    if not total:
        log.info("No card_ids found in fut_players.")
        return (0, 0, 0)

    # Shuffle so we don't hammer the same range in the same order every run
    random.shuffle(card_ids)
    log.info("Refreshing prices for %s cards (platform=%s, concurrency=%s)...", total, PLATFORM, CONCURRENCY)

    sem = asyncio.Semaphore(CONCURRENCY)

    async def work(cid_str: str) -> Tuple[str, Optional[int]]:
        try:
            cid = int(cid_str)
        except Exception:
            return (cid_str, None)
        async with sem:
            price = await _get_price_with_retries(cid, PLATFORM)
            # tiny courtesy jitter between requests
            await asyncio.sleep(0.01)
            return (cid_str, price)

    results: List[Tuple[str, Optional[int]]] = []

    for i in range(0, total, BATCH_SIZE):
        if _stop.is_set():
            break
        chunk = card_ids[i:i + BATCH_SIZE]
        chunk_results = await asyncio.gather(*(work(cid) for cid in chunk))
        results.extend(chunk_results)
        log.info("Fetched prices for %s/%s cards...", min(i + BATCH_SIZE, total), total)

    # Prepare updates
    update_rows = [(r[1], r[0]) for r in results if isinstance(r[1], int)]
    missing = len([1 for _, p in results if p is None])

    updated = 0
    if update_rows:
        async with pool.acquire() as conn:
            # executemany is efficient for many simple updates
            await conn.executemany(
                "UPDATE fut_players SET price = $1 WHERE card_id = $2::text",
                update_rows
            )
            updated = len(update_rows)

    log.info("Pass finished. Updated: %s | Missing/failed: %s | Total: %s", updated, missing, total)
    return (total, updated, missing)


async def main_loop():
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    try:
        while not _stop.is_set():
            start = time.monotonic()
            try:
                await _refresh_once(pool)
            except Exception as e:
                log.exception("Unexpected error during refresh pass: %s", e)

            # Sleep until next interval (account for time spent)
            elapsed = time.monotonic() - start
            remaining = max(0, INTERVAL_SECONDS - int(elapsed))
            if remaining:
                log.info("Sleeping %ss until next run...", remaining)

            try:
                await asyncio.wait_for(_stop.wait(), timeout=remaining if remaining else 0.0)
            except asyncio.TimeoutError:
                # timeout means do another loop
                pass
    finally:
        await pool.close()
        log.info("Pool closed. Bye.")


if __name__ == "__main__":
    asyncio.run(main_loop())
