# scripts/retention_cleanup.py
import os
import asyncio
import logging
from datetime import timedelta
import asyncpg

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is required")

# Retention windows (override via Railway Variables if you want)
RET_TICKS_DAYS         = int(os.getenv("RET_TICKS_DAYS", "30"))
RET_CANDLES_15M_DAYS   = int(os.getenv("RET_CANDLES_15M_DAYS", "180"))
RET_PRICEHIST_DAYS     = int(os.getenv("RET_PRICEHIST_DAYS", "90"))

# Delete in small chunks to keep locks short
BATCH_ROWS = int(os.getenv("RETENTION_BATCH_ROWS", "20000"))

SQLS = {
    "ticks": f"""
        DELETE FROM public.fut_ticks
        WHERE ts < NOW() - INTERVAL '{RET_TICKS_DAYS} days'
        LIMIT {BATCH_ROWS};
    """,
    "candles_15m": f"""
        DELETE FROM public.fut_candles
        WHERE timeframe='15m'
          AND open_time < NOW() - INTERVAL '{RET_CANDLES_15M_DAYS} days'
        LIMIT {BATCH_ROWS};
    """,
    "pricehist": f"""
        DELETE FROM public.fut_prices_history
        WHERE captured_at < NOW() - INTERVAL '{RET_PRICEHIST_DAYS} days'
        LIMIT {BATCH_ROWS};
    """,
}

async def delete_in_batches(conn: asyncpg.Connection, label: str, sql: str) -> int:
    total = 0
    while True:
        n = await conn.execute(sql)
        # asyncpg returns like "DELETE 1234"
        try:
            count = int(n.split()[-1])
        except Exception:
            count = 0
        total += count
        if count < BATCH_ROWS:
            break
    logging.info("Retention %s: deleted %s rows", label, total)
    return total

async def main():
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3)
    try:
        async with pool.acquire() as conn:
            # ensure we're in public schema (if your app already sets this, it’s fine)
            await conn.execute("SET search_path TO public")

            await delete_in_batches(conn, "ticks", SQLS["ticks"])
            await delete_in_batches(conn, "candles_15m", SQLS["candles_15m"])
            await delete_in_batches(conn, "pricehist", SQLS["pricehist"])

            # optional: touch a heartbeat table so you can monitor it
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS public.retention_heartbeat (
                  id boolean primary key default true,
                  last_run_at timestamptz not null default now(),
                  ticks_deleted bigint not null default 0,
                  candles_deleted bigint not null default 0,
                  history_deleted bigint not null default 0
                );
            """)
            # NOTE: You could pass actual totals if you store them from above (left simple here)
            await conn.execute("""
                INSERT INTO public.retention_heartbeat (id, last_run_at)
                VALUES (TRUE, now())
                ON CONFLICT (id) DO UPDATE SET last_run_at=excluded.last_run_at;
            """)
    finally:
        await pool.close()

if __name__ == "__main__":
    asyncio.run(main())