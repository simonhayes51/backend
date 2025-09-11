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

# Platforms to record in history. FUT.GG endpoint isn't per-platform,
# but we store the same snapshot under each for convenience.
PLATFORMS = [p.strip().lower() for p in os.getenv("PLATFORMS", "ps").split(",") if p.strip()]

# Tuning knobs
CONCURRENCY = int(os.getenv("REFRESH_CONCURRENCY", "16"))
BATCH_SIZE  = int(os.getenv("REFRESH_BATCH_SIZE", "500"))
SLEEP_SECS  = int(os.getenv("REFRESH_INTERVAL_SEC", "1800"))  # 30 minutes

FUTGG_PRICE_URL = "https://www.fut.gg/api/fut/player-prices/26/{card_id}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en;q=0.9",
    "Referer": "https://www.fut.gg/",
    "Origin": "https://www.fut.gg",
}

# ----------------------- migrations (TEXT ids, TEXT price, TEXT platform) -----------------------
DDL_STMTS = [
    # Ensure fut_players exists (minimal) — harmless if already present.
    """
    CREATE TABLE IF NOT EXISTS fut_players (
        card_id TEXT PRIMARY KEY
    );
    """,
    # Ensure snapshot columns (TEXT price, TIMESTAMPTZ price_updated_at).
    """
    ALTER TABLE fut_players
        ADD COLUMN IF NOT EXISTS price TEXT,
        ADD COLUMN IF NOT EXISTS price_updated_at TIMESTAMPTZ;
    """,
    # Coerce fut_players.price to TEXT if created differently earlier.
    """
    DO $$
    BEGIN
      IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='fut_players' AND column_name='price' AND data_type <> 'text'
      ) THEN
        EXECUTE 'ALTER TABLE fut_players ALTER COLUMN price TYPE TEXT USING price::text';
      END IF;
    END$$;
    """,
    # History table: everything TEXT except timestamp.
    """
    CREATE TABLE IF NOT EXISTS fut_prices_history (
        id BIGSERIAL PRIMARY KEY,
        card_id  TEXT NOT NULL,
        platform TEXT NOT NULL,
        price    TEXT NOT NULL,
        captured_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_prices_history_card_time
      ON fut_prices_history(card_id, platform, captured_at DESC);
    """,
    # Coerce history columns if they were created with other types.
    """
    DO $$
    BEGIN
      IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='fut_prices_history' AND column_name='card_id' AND data_type <> 'text'
      ) THEN
        EXECUTE 'ALTER TABLE fut_prices_history ALTER COLUMN card_id TYPE TEXT USING card_id::text';
      END IF;

      IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='fut_prices_history' AND column_name='platform' AND data_type <> 'text'
      ) THEN
        EXECUTE 'ALTER TABLE fut_prices_history ALTER COLUMN platform TYPE TEXT USING platform::text';
      END IF;

      IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='fut_prices_history' AND column_name='price' AND data_type <> 'text'
      ) THEN
        EXECUTE 'ALTER TABLE fut_prices_history ALTER COLUMN price TYPE TEXT USING price::text';
      END IF;
    END$$;
    """,
]

async def run_migrations(pool: asyncpg.Pool):
    async with pool.acquire() as conn:
        for stmt in DDL_STMTS:
            await conn.execute(stmt)
    logging.info("Migrations applied (idempotent; TEXT ids/prices/platform).")

# ----------------------- fetching -----------------------
async def _fetch_one(session: aiohttp.ClientSession, card_id: str) -> Optional[str]:
    """
    Returns the price as STRING (for TEXT storage), or None.
    """
    url = FUTGG_PRICE_URL.format(card_id=card_id)
    try:
        async with session.get(url, timeout=15) as r:
            if r.status != 200:
                return None
            data = await r.json()
            current = (data.get("data") or {}).get("currentPrice") or {}
            price = current.get("price")
            if price is None:
                return None
            # Normalise to string regardless of type
            return str(price)
    except Exception:
        return None

async def _fetch_prices_for_ids(card_ids: List[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    sem = asyncio.Semaphore(CONCURRENCY)
    async with aiohttp.ClientSession(headers=HEADERS) as sess:
        async def worker(cid: str):
            async with sem:
                price_s = await _fetch_one(sess, cid)
                if isinstance(price_s, str) and price_s.strip():
                    out[cid] = price_s.strip()
        tasks = [asyncio.create_task(worker(cid)) for cid in card_ids]
        await asyncio.gather(*tasks, return_exceptions=True)
    return out

# ----------------------- db helpers -----------------------
async def _load_card_ids(conn: asyncpg.Connection) -> List[str]:
    rows = await conn.fetch("SELECT card_id FROM fut_players")
    return [str(r["card_id"]) for r in rows if r["card_id"] is not None]

async def _update_snapshot(conn: asyncpg.Connection, prices: List[Tuple[str, str]]):
    """
    prices: List of (price_text, card_id_text)
    """
    sql = """
        UPDATE fut_players
        SET price = $1::text,
            price_updated_at = NOW()
        WHERE card_id = $2::text
    """
    await conn.executemany(sql, prices)

async def _insert_history(conn: asyncpg.Connection, rows: List[Tuple[str, str, str]]):
    """
    rows: List of (card_id_text, platform_text, price_text)
    """
    sql = """
        INSERT INTO fut_prices_history (card_id, platform, price, captured_at)
        VALUES ($1::text, $2::text, $3::text, NOW())
    """
    await conn.executemany(sql, rows)

# ----------------------- refresh pass -----------------------
async def _refresh_once(pool: asyncpg.Pool):
    async with pool.acquire() as conn:
        card_ids = await _load_card_ids(conn)
    total = len(card_ids)
    if total == 0:
        logging.warning("No card_ids found in fut_players.")
        return
    logging.info("Starting refresh for %s cards on platforms=%s ...", total, ",".join(PLATFORMS))

    prices_map: Dict[str, str] = {}
    for i in range(0, total, BATCH_SIZE):
        chunk = card_ids[i:i+BATCH_SIZE]
        chunk_prices = await _fetch_prices_for_ids(chunk)
        prices_map.update(chunk_prices)
        logging.info("Fetched %s/%s price points...", len(prices_map), total)

    if not prices_map:
        logging.warning("No prices fetched this pass.")
        return

    # Update fut_players snapshot (TEXT price + TEXT id)
    pairs_snapshot: List[Tuple[str, str]] = [(str(price_text), str(cid)) for cid, price_text in prices_map.items()]
    async with pool.acquire() as conn:
        await _update_snapshot(conn, pairs_snapshot)

    # Append history for each platform (all TEXT)
    history_rows: List[Tuple[str, str, str]] = []
    for cid, price_text in prices_map.items():
        for plat in PLATFORMS:
            history_rows.append((str(cid), str(plat).lower(), str(price_text)))

    async with pool.acquire() as conn:
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
