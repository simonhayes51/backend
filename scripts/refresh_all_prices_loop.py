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

# Platforms to log (even though FUT.GG isn't per-platform,
# we still store one tick per platform so the rest of the app is uniform)
PLATFORMS = [p.strip().lower() for p in os.getenv("PLATFORMS", "ps").split(",") if p.strip()]

# Tuning knobs
# Tuning knobs
CONCURRENCY = int(os.getenv("REFRESH_CONCURRENCY", "32"))   # double concurrency
BATCH_SIZE  = int(os.getenv("REFRESH_BATCH_SIZE", "1000"))  # double batch
SLEEP_SECS  = int(os.getenv("REFRESH_INTERVAL_SEC", "300")) # run every 5 min
FUTGG_PRICE_URL = "https://www.fut.gg/api/fut/player-prices/26/{card_id}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en;q=0.9",
    "Referer": "https://www.fut.gg/",
    "Origin": "https://www.fut.gg",
}

# ----------------------- migrations (adds ticks+candles) -----------------------
DDL_STMTS = [
    # minimal fut_players (keeps your existing columns; we won't drop anything)
    """
    CREATE TABLE IF NOT EXISTS public.fut_players (
        card_id TEXT PRIMARY KEY
    );
    """,
    # ensure snapshot columns exist; keep TEXT price for backward-compat
    """
    ALTER TABLE public.fut_players
        ADD COLUMN IF NOT EXISTS price TEXT,
        ADD COLUMN IF NOT EXISTS price_num BIGINT,
        ADD COLUMN IF NOT EXISTS price_updated_at TIMESTAMPTZ;
    """,
    # history table (your existing one; TEXT columns are fine)
    """
    CREATE TABLE IF NOT EXISTS public.fut_prices_history (
        id BIGSERIAL PRIMARY KEY,
        card_id  TEXT NOT NULL,
        platform TEXT NOT NULL,
        price    TEXT NOT NULL,
        captured_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_prices_history_card_time ON public.fut_prices_history(card_id, platform, captured_at DESC);",

    # --- NEW: ticks (what charts/AI want as the raw feed) ---
    """
    CREATE TABLE IF NOT EXISTS public.fut_ticks (
        id BIGSERIAL PRIMARY KEY,
        player_card_id TEXT NOT NULL REFERENCES public.fut_players(card_id) ON DELETE CASCADE,
        platform TEXT NOT NULL CHECK (platform IN ('ps','xbox')),
        ts TIMESTAMPTZ NOT NULL,
        price INT NOT NULL
    );
    """,
    "CREATE INDEX IF NOT EXISTS fut_ticks_player_idx ON public.fut_ticks (player_card_id, platform, ts);",

    # --- NEW: candles (OHLC) ---
    """
    CREATE TABLE IF NOT EXISTS public.fut_candles (
        id BIGSERIAL PRIMARY KEY,
        player_card_id TEXT NOT NULL REFERENCES public.fut_players(card_id) ON DELETE CASCADE,
        platform TEXT NOT NULL CHECK (platform IN ('ps','xbox')),
        timeframe TEXT NOT NULL,
        open_time TIMESTAMPTZ NOT NULL,
        open  INT NOT NULL,
        high  INT NOT NULL,
        low   INT NOT NULL,
        close INT NOT NULL,
        volume INT NOT NULL DEFAULT 0
    );
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS fut_candles_unq
      ON public.fut_candles (player_card_id, platform, timeframe, open_time);
    """,
]

async def run_migrations(pool: asyncpg.Pool):
    async with pool.acquire() as conn:
        for stmt in DDL_STMTS:
            await conn.execute(stmt)
    logging.info("Migrations applied (players, history, ticks, candles).")

# ----------------------- fetching -----------------------
async def _fetch_one(session: aiohttp.ClientSession, card_id: str) -> Optional[str]:
    url = FUTGG_PRICE_URL.format(card_id=card_id)
    try:
        async with session.get(url, timeout=15) as r:
            if r.status != 200:
                return None
            data = await r.json()
            cur = (data.get("data") or {}).get("currentPrice") or {}
            price = cur.get("price")
            if price is None:
                return None
            return str(price)  # always return TEXT
    except Exception:
        return None

async def _fetch_prices_for_ids(card_ids: List[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    sem = asyncio.Semaphore(CONCURRENCY)
    async with aiohttp.ClientSession(headers=HEADERS) as sess:
        async def worker(cid: str):
            async with sem:
                p = await _fetch_one(sess, cid)
                if isinstance(p, str) and p.strip():
                    out[cid] = p.strip()
        tasks = [asyncio.create_task(worker(cid)) for cid in card_ids]
        await asyncio.gather(*tasks, return_exceptions=True)
    return out

# ----------------------- db helpers -----------------------
async def _load_card_ids(conn: asyncpg.Connection) -> List[str]:
    rows = await conn.fetch("SELECT card_id FROM public.fut_players")
    return [str(r["card_id"]) for r in rows if r["card_id"] is not None]

async def _update_snapshot(conn: asyncpg.Connection, pairs: List[Tuple[str, str]]):
    # pairs: (price_text, card_id)
    sql = """
        UPDATE public.fut_players
        SET price = $1::text,
            price_num = NULLIF($1, '')::bigint,
            price_updated_at = NOW()
        WHERE card_id = $2::text
    """
    await conn.executemany(sql, pairs)

async def _insert_history(conn: asyncpg.Connection, rows: List[Tuple[str, str, str]]):
    # rows: (card_id, platform, price_text)
    sql = """
        INSERT INTO public.fut_prices_history (card_id, platform, price, captured_at)
        VALUES ($1::text, $2::text, $3::text, NOW())
    """
    await conn.executemany(sql, rows)

async def _insert_ticks(conn: asyncpg.Connection, rows: List[Tuple[str, str, int]]):
    # rows: (player_card_id, platform, price_int)
    sql = """
        INSERT INTO public.fut_ticks (player_card_id, platform, ts, price)
        VALUES ($1::text, $2::text, NOW(), $3::int)
    """
    await conn.executemany(sql, rows)

async def _rollup_15m(conn: asyncpg.Connection, since_hours: int = 8):
    """
    Rebuild/refresh 15m candles for the recent window from ticks.
    Idempotent via ON CONFLICT.
    """
    await conn.execute(
        """
        WITH t AS (
          SELECT
            player_card_id,
            platform,
            to_timestamp(floor(extract(epoch FROM ts) / 900) * 900) AT TIME ZONE 'UTC' AS bucket,
            ts, price
          FROM public.fut_ticks
          WHERE ts >= NOW() - ($1 || ' hours')::interval
        ),
        agg AS (
          SELECT
            player_card_id,
            platform,
            bucket AS open_time,
            (array_agg(price ORDER BY ts ASC))[1]  AS open,
            max(price)                             AS high,
            min(price)                             AS low,
            (array_agg(price ORDER BY ts ASC))[array_length(array_agg(price),1)] AS close,
            count(*)                               AS volume
          FROM t
          GROUP BY player_card_id, platform, bucket
        )
        INSERT INTO public.fut_candles (player_card_id, platform, timeframe, open_time, open, high, low, close, volume)
        SELECT player_card_id, platform, '15m', open_time, open, high, low, close, volume
        FROM agg
        ON CONFLICT (player_card_id, platform, timeframe, open_time)
        DO UPDATE SET open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low,
                      close=EXCLUDED.close, volume=EXCLUDED.volume;
        """,
        since_hours,
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

    prices_map: Dict[str, str] = {}
    for i in range(0, total, BATCH_SIZE):
        chunk = card_ids[i:i+BATCH_SIZE]
        chunk_prices = await _fetch_prices_for_ids(chunk)
        prices_map.update(chunk_prices)
        logging.info("Fetched %s/%s price points...", len(prices_map), total)

    if not prices_map:
        logging.warning("No prices fetched this pass.")
        return

    # 1) Update fut_players snapshot (TEXT + price_num BIGINT)
    pairs_snapshot: List[Tuple[str, str]] = [(p, cid) for cid, p in prices_map.items()]
    async with pool.acquire() as conn:
        await _update_snapshot(conn, pairs_snapshot)

    # 2) Append history (TEXT) and 3) Append ticks (INT) for each platform
    hist_rows: List[Tuple[str, str, str]] = []
    tick_rows: List[Tuple[str, str, int]] = []

    for cid, ptxt in prices_map.items():
        # safe integer cast; skip bad values
        try:
            pint = int(ptxt.replace(",", "").strip())
        except Exception:
            continue
        for plat in PLATFORMS:
            hist_rows.append((cid, plat, ptxt))
            tick_rows.append((cid, plat, pint))

    async with pool.acquire() as conn:
        # history in chunks
        for j in range(0, len(hist_rows), 5000):
            await _insert_history(conn, hist_rows[j:j+5000])
        # ticks in chunks
        for j in range(0, len(tick_rows), 5000):
            await _insert_ticks(conn, tick_rows[j:j+5000])
        # roll up last N hours into 15m candles (tune the window if you like)
        await _rollup_15m(conn, since_hours=48)

    logging.info(
        "Updated fut_players (%s), wrote %s history rows and %s ticks, rolled 15m candles.",
        len(pairs_snapshot), len(hist_rows), len(tick_rows),
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