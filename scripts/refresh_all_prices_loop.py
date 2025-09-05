# scripts/refresh_all_prices_loop.py
import os
import asyncio
import logging
import time
from typing import Dict, List, Tuple, Optional

import aiohttp
import asyncpg
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is required")

# Platforms to record in history. FUT.GG price endpoint isn't per-platform,
# but we store the same snapshot under each for convenience.
PLATFORMS = [p.strip().lower() for p in os.getenv("PLATFORMS", "ps").split(",") if p.strip()]

# Tuning knobs
CONCURRENCY = int(os.getenv("REFRESH_CONCURRENCY", "16"))
BATCH_SIZE  = int(os.getenv("REFRESH_BATCH_SIZE", "500"))
SLEEP_SECS  = int(os.getenv("REFRESH_INTERVAL_SEC", "1800"))  # 30 minutes

FUTGG_PRICE_URL = "https://www.fut.gg/api/fut/player-prices/25/{card_id}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en;q=0.9",
    "Referer": "https://www.fut.gg/",
    "Origin": "https://www.fut.gg",
}

# ----------------------- migrations -----------------------
DDL_STMTS = [
    # add live snapshot columns to fut_players
    """
    ALTER TABLE fut_players
        ADD COLUMN IF NOT EXISTS price INTEGER,
        ADD COLUMN IF NOT EXISTS price_updated_at TIMESTAMPTZ
    """,
    # historical table
    """
    CREATE TABLE IF NOT EXISTS fut_prices_history (
        id BIGSERIAL PRIMARY KEY,
        card_id TEXT NOT NULL,
        platform VARCHAR(10) NOT NULL,
        price INTEGER NOT NULL,
        captured_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_prices_history_card_time
        ON fut_prices_history(card_id, platform, captured_at DESC)
    """,
]

async def run_migrations(pool: asyncpg.Pool):
    async with pool.acquire() as conn:
        for stmt in DDL_STMTS:
            await conn.execute(stmt)
    logging.info("Migrations applied (idempotent).")

# ----------------------- fetching -----------------------
async def _fetch_one(session: aiohttp.ClientSession, card_id: str) -> Optional[int]:
    url = FUTGG_PRICE_URL.format(card_id=card_id)
    try:
        async with session.get(url, timeout=15) as r:
            if r.status != 200:
                return None
            data = await r.json()
            current = (data.get("data") or {}).get("currentPrice") or {}
            price = current.get("price")
            if isinstance(price, (int, float)):
                return int(price)
            return None
    except Exception:
        return None

async def _fetch_prices_for_ids(card_ids: List[str]) -> Dict[str, int]:
    # Single session reused to reduce overhead
    out: Dict[str, int] = {}
    sem = asyncio.Semaphore(CONCURRENCY)
    async with aiohttp.ClientSession(headers=HEADERS) as sess:
        async def worker(cid: str):
            async with sem:
                price = await _fetch_one(sess, cid)
                if isinstance(price, int):
                    out[cid] = price
        tasks = [asyncio.create_task(worker(cid)) for cid in card_ids]
        await asyncio.gather(*tasks, return_exceptions=True)
    return out

# ----------------------- db helpers -----------------------
async def _load_card_ids(conn: asyncpg.Connection) -> List[str]:
    rows = await conn.fetch("SELECT card_id FROM fut_players")
    # Ensure TEXT binding by casting to str
    return [str(r["card_id"]) for r in rows if r["card_id"] is not None]

async def _update_snapshot(conn: asyncpg.Connection, prices: List[Tuple[int, str]]):
    # ($1 int price, $2 text card_id)
    await conn.executemany(
        "UPDATE fut_players SET price=$1, price_updated_at=NOW() WHERE card_id=$2::text",
        prices,
    )

async def _insert_history(conn: asyncpg.Connection, rows: List[Tuple[str, str, int]]):
    # ($1 text card_id, $2 text platform, $3 int price)
    await conn.executemany(
        "INSERT INTO fut_prices_history (card_id, platform, price, captured_at) VALUES ($1,$2,$3, NOW())",
        rows,
    )

# ----------------------- refresh pass -----------------------
async def _refresh_once(pool: asyncpg.Pool):
    async with pool.acquire() as conn:
        card_ids = await _load_card_ids(conn)
    total = len(card_ids)
    if total == 0:
        logging.warning("No card_ids found in fut_players.")
        return
    logging.info("Starting refresh for %s cards on platforms=%s ...", total, ",".join(PLATFORMS))

    prices_map: Dict[str, int] = {}
    for i in range(0, total, BATCH_SIZE):
        chunk = card_ids[i:i+BATCH_SIZE]
        chunk_prices = await _fetch_prices_for_ids(chunk)
        prices_map.update(chunk_prices)
        logging.info("Fetched %s/%s price points...", len(prices_map), total)

    if not prices_map:
        logging.warning("No prices fetched this pass.")
        return

    # Update fut_players snapshot
    pairs_snapshot: List[Tuple[int, str]] = [(price, cid) for cid, price in prices_map.items()]
    async with pool.acquire() as conn:
        await _update_snapshot(conn, pairs_snapshot)

    # Append history for each platform
    history_rows: List[Tuple[str, str, int]] = []
    for cid, price in prices_map.items():
        for plat in PLATFORMS:
            history_rows.append((cid, plat, price))

    async with pool.acquire() as conn:
        # chunk to reasonable sizes
        for j in range(0, len(history_rows), 5000):
            await _insert_history(conn, history_rows[j:j+5000])

    logging.info(
        "Updated fut_players (%s) and appended %s history rows.",
        len(pairs_snapshot), len(history_rows)
    )

# ----------------------- main loop -----------------------
async def main_loop():
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    try:
        await run_migrations(pool)
        while True:
            started = time.time()
            try:
                await _refresh_once(pool)
            except Exception as e:
                logging.exception("Unexpected error during refresh pass: %s", e)
            elapsed = time.time() - started
            sleep_for = max(5, SLEEP_SECS - int(elapsed))
            logging.info("Sleeping %ss until next run...", sleep_for)
            await asyncio.sleep(sleep_for)
    finally:
        await pool.close()

if __name__ == "__main__":
    asyncio.run(main_loop())
