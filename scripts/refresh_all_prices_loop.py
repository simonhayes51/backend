# scripts/refresh_all_prices_loop.py
import os
import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import asyncpg

# Import your existing price fetcher
# Requires PYTHONPATH=. in the worker service env
from app.services.prices import get_player_price  # type: ignore

# ---------------- Env & Defaults ----------------
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is required")

REFRESH_PLATFORM = os.getenv("REFRESH_PLATFORM", "ps").lower()  # ps | xbox | pc
REFRESH_CONCURRENCY = int(os.getenv("REFRESH_CONCURRENCY", "20"))
REFRESH_RETRIES = int(os.getenv("REFRESH_RETRIES", "2"))
REFRESH_BATCH_SIZE = int(os.getenv("REFRESH_BATCH_SIZE", "1000"))
REFRESH_INTERVAL_SECONDS = int(os.getenv("REFRESH_INTERVAL_SECONDS", "1800"))  # 30 min
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
)

# ---------------- Helpers ----------------
def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

def _chunks(seq: List[str], n: int) -> List[List[str]]:
    return [seq[i : i + n] for i in range(0, len(seq), n)]

async def _ensure_schema(conn: asyncpg.Connection) -> None:
    # Add price_updated_at if you haven't already
    await conn.execute(
        """
        ALTER TABLE fut_players
        ADD COLUMN IF NOT EXISTS price_updated_at TIMESTAMPTZ
        """
    )

async def _fetch_all_card_ids(conn: asyncpg.Connection) -> List[str]:
    # card_id is TEXT in your schema elsewhere; fetch as TEXT always.
    rows = await conn.fetch("SELECT card_id FROM fut_players")
    ids: List[str] = []
    for r in rows:
        cid = r["card_id"]
        if cid is None:
            continue
        # Normalize to str
        ids.append(str(cid))
    return ids

async def _fetch_one_price(card_id_text: str, platform: str, retries: int) -> Optional[int]:
    """
    Get price for a single card_id (string in DB). Your get_player_price expects int.
    If card_id isn't numeric, skip it gracefully.
    """
    try:
        card_id_int = int(card_id_text)
    except Exception:
        return None

    delay = 0.5
    for attempt in range(retries + 1):
        try:
            price = await get_player_price(card_id_int, platform)
            if isinstance(price, (int, float)):
                return int(price)
            return None
        except Exception as e:
            if attempt >= retries:
                logging.debug("price fetch failed for %s (%s): %s", card_id_text, platform, e)
                return None
            await asyncio.sleep(delay)
            delay *= 2  # simple backoff

async def _gather_prices(
    ids: List[str],
    platform: str,
    concurrency: int,
    retries: int,
) -> Dict[str, Optional[int]]:
    """
    Returns { card_id_text: price_or_None }
    """
    sem = asyncio.Semaphore(concurrency)
    out: Dict[str, Optional[int]] = {}

    async def worker(cid: str):
        async with sem:
            out[cid] = await _fetch_one_price(cid, platform, retries)

    await asyncio.gather(*(worker(cid) for cid in ids))
    return out

async def _update_prices(
    conn: asyncpg.Connection,
    price_map: Dict[str, Optional[int]],
) -> Tuple[int, int]:
    """
    Batch update fut_players:
    SQL expects ($1 card_id TEXT, $2 price INT, $3 timestamp)
    """
    now = _now_utc()
    rows: List[Tuple[str, int, datetime]] = []
    for cid, price in price_map.items():
        if isinstance(price, int):
            rows.append((cid, price, now))

    if not rows:
        return 0, 0

    # Use executemany with correct param order/types
    await conn.executemany(
        """
        UPDATE fut_players
        SET price = $2, price_updated_at = $3
        WHERE card_id = $1::text
        """,
        rows,
    )
    return len(price_map), len(rows)

async def _refresh_once(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await _ensure_schema(conn)

        ids = await _fetch_all_card_ids(conn)
        total_ids = len(ids)
        if total_ids == 0:
            logging.info("No fut_players found. Nothing to refresh.")
            return

        logging.info("Starting refresh for %s cards on platform=%s ...", total_ids, REFRESH_PLATFORM)

        updated = 0
        processed = 0

        # Process in batches so logs stay readable and memory stays flat
        for batch in _chunks(ids, REFRESH_BATCH_SIZE):
            prices = await _gather_prices(batch, REFRESH_PLATFORM, REFRESH_CONCURRENCY, REFRESH_RETRIES)
            # Persist batch
            async with pool.acquire() as wconn:
                _, updated_cnt = await _update_prices(wconn, prices)
            updated += updated_cnt
            processed += len(batch)
            logging.info("Progress: %s/%s processed, %s updated", processed, total_ids, updated)

        logging.info("Refresh pass complete: processed=%s, updated=%s", processed, updated)

async def main_loop() -> None:
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    try:
        while True:
            try:
                await _refresh_once(pool)
            except Exception as e:
                logging.error("Unexpected error during refresh pass: %s", e, exc_info=True)

            # Sleep until next pass
            logging.info("Sleeping %ss until next run...", REFRESH_INTERVAL_SECONDS)
            await asyncio.sleep(REFRESH_INTERVAL_SECONDS)
    finally:
        await pool.close()

if __name__ == "__main__":
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        logging.info("Shutting down refresher loop...")
