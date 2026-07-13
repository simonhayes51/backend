"""
Fair Value engine — the precomputed layer over sales_history + bin_history.

fair_value_mv (migrations/011_player_fair_value.sql) holds, per card:
real median sold price (24h/7d), liquidity (sales/hour), volatility,
current PS BIN, discount vs fair value, and a BIN z-score. This module:

  1. ensures the matview exists (idempotent, for deploys that haven't run
     the migration yet),
  2. refreshes it periodically under a pg advisory lock (safe with
     multiple app instances),
  3. exposes the read queries used by app/routers/fair_value.py and the
     public v2 API.

Reads come from the PLAYER database pool — sales/bin history live there.
"""
from __future__ import annotations

import asyncio
import logging
import pathlib
from typing import Any, Dict, List, Optional

import asyncpg
from asyncpg import exceptions as asyncpg_exceptions

log = logging.getLogger("fair_value")

REFRESH_LOCK_KEY = 7741002  # distinct from the candle-aggregator lock
_MIGRATION_FILE = (
    pathlib.Path(__file__).resolve().parent.parent.parent
    / "migrations"
    / "011_player_fair_value.sql"
)


async def ensure_fair_value_mv(pool: asyncpg.Pool) -> bool:
    """Create the matview + indexes if missing. Returns True if usable.

    Guarded by the same advisory lock as refresh_fair_value: on a rolling
    deploy, two instances can boot within the same window, and without a
    shared lock one instance's CREATE can be mid-flight (not yet committed)
    when the other's REFRESH runs on a different connection - Postgres has
    no visibility into an uncommitted DDL from another session, so that
    REFRESH genuinely sees 'relation does not exist' (this is what fired
    live: a REFRESH statement erroring 13 seconds before the next refresh
    cycle succeeded once the create had landed). pg_try_advisory_lock makes
    the two operations mutually exclusive instead of racing.
    """
    try:
        async with pool.acquire() as conn:
            got = await conn.fetchval("SELECT pg_try_advisory_lock($1)", REFRESH_LOCK_KEY)
            if not got:
                # Another instance is creating/refreshing right now -
                # assume it has this covered rather than blocking here.
                return True
            try:
                exists = await conn.fetchval(
                    "SELECT 1 FROM pg_matviews WHERE matviewname = 'fair_value_mv'"
                )
                if exists:
                    return True
                log.info("fair_value_mv missing - creating from migration 011")
                await conn.execute(_MIGRATION_FILE.read_text())
                return True
            finally:
                await conn.execute("SELECT pg_advisory_unlock($1)", REFRESH_LOCK_KEY)
    except Exception as e:
        log.error("ensure_fair_value_mv failed: %s", e)
        return False


async def refresh_fair_value(pool: asyncpg.Pool) -> bool:
    """One guarded refresh. Concurrent-safe across instances. Self-heals if
    the matview turns out to be missing (recreates and retries once)
    instead of failing every cycle forever."""
    try:
        async with pool.acquire() as conn:
            got = await conn.fetchval("SELECT pg_try_advisory_lock($1)", REFRESH_LOCK_KEY)
            if not got:
                return False
            try:
                try:
                    await conn.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY fair_value_mv")
                except asyncpg_exceptions.UndefinedTableError:
                    log.warning("fair_value_mv missing at refresh time - recreating")
                    await conn.execute(_MIGRATION_FILE.read_text())
                    await conn.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY fair_value_mv")
                return True
            finally:
                await conn.execute("SELECT pg_advisory_unlock($1)", REFRESH_LOCK_KEY)
    except Exception as e:
        log.error("fair_value refresh failed: %s", e)
        return False


async def refresher_loop(pool: asyncpg.Pool, interval_seconds: int = 300) -> None:
    """Background task started from the app lifespan.

    Waits a few seconds before its first attempt. During rapid iterative
    redeploys a container can be superseded (SIGTERM) within seconds of
    starting; without this delay the very first iteration - potentially a
    multi-statement matview (re)create - can be cancelled mid-flight,
    which Postgres logs as a real-looking ERROR even though it's a benign
    side effect of the shutdown (an interrupted CREATE just rolls back,
    nothing is left inconsistent). This doesn't eliminate the race, just
    makes it far less likely to catch a short-lived container mid-DDL.
    """
    await asyncio.sleep(5)
    while True:
        try:
            ok = await ensure_fair_value_mv(pool)
            if ok:
                await refresh_fair_value(pool)
            else:
                log.warning("fair_value_mv unavailable this cycle - will retry")
        except Exception as e:  # never let the loop die
            log.error("fair_value refresher iteration failed: %s", e)
        await asyncio.sleep(interval_seconds)


# ----------------------------- read queries ---------------------------------

_ROW_COLS = """
    card_id, name, rating, version, position, image_url,
    fair_value_24h, fair_value_7d, sales_24h, sales_7d,
    sales_per_hour_24h, volatility_24h, last_sale_at,
    current_bin, bin_captured_at, discount_pct, bin_zscore_24h,
    data_quality_suspect, computed_at
"""


def _row_to_dict(r: asyncpg.Record) -> Dict[str, Any]:
    d = dict(r)
    for k in ("last_sale_at", "bin_captured_at", "computed_at"):
        if d.get(k) is not None:
            d[k] = d[k].isoformat()
    for k in ("sales_per_hour_24h", "discount_pct", "bin_zscore_24h"):
        if d.get(k) is not None:
            d[k] = float(d[k])
    return d


async def get_card_fair_value(pool: asyncpg.Pool, card_id: int) -> Optional[Dict[str, Any]]:
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            f"SELECT {_ROW_COLS} FROM fair_value_mv WHERE card_id = $1", card_id
        )
    return _row_to_dict(r) if r else None


async def get_card_fair_values_batch(
    pool: asyncpg.Pool, card_ids: List[int]
) -> List[Dict[str, Any]]:
    """Same shape as get_card_fair_value but for many cards in one query -
    lets a caller showing N rows at once (e.g. a search results page) make
    one request instead of N."""
    if not card_ids:
        return []
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT {_ROW_COLS} FROM fair_value_mv WHERE card_id = ANY($1::bigint[])",
            list(card_ids),
        )
    return [_row_to_dict(r) for r in rows]


async def get_undervalued(
    pool: asyncpg.Pool,
    *,
    limit: int = 30,
    min_price: int = 1000,
    max_price: Optional[int] = None,
    min_sales_24h: int = 5,
    min_discount_pct: float = 3.0,
) -> List[Dict[str, Any]]:
    """The 'Undervalued right now' board: cards whose live BIN sits below the
    real 24h median sold price, with enough liquidity to actually exit."""
    where = [
        "current_bin IS NOT NULL",
        "fair_value_24h IS NOT NULL",
        "NOT data_quality_suspect",
        "sales_24h >= $2",
        "discount_pct >= $3",
        "current_bin >= $4",
    ]
    params: List[Any] = [limit, min_sales_24h, min_discount_pct, min_price]
    if max_price is not None:
        params.append(max_price)
        where.append(f"current_bin <= ${len(params)}")

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT {_ROW_COLS} FROM fair_value_mv
            WHERE {' AND '.join(where)}
            ORDER BY discount_pct DESC
            LIMIT $1
            """,
            *params,
        )
    return [_row_to_dict(r) for r in rows]


async def get_anomalies(
    pool: asyncpg.Pool,
    *,
    limit: int = 30,
    zscore_threshold: float = -2.0,
    min_sales_24h: int = 8,
) -> List[Dict[str, Any]]:
    """Statistical outliers: BIN more than |z| standard deviations below the
    24h sold distribution on liquid cards - the 'snipe radar'."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT {_ROW_COLS} FROM fair_value_mv
            WHERE bin_zscore_24h IS NOT NULL
              AND NOT data_quality_suspect
              AND bin_zscore_24h <= $2
              AND sales_24h >= $3
            ORDER BY bin_zscore_24h ASC
            LIMIT $1
            """,
            limit, zscore_threshold, min_sales_24h,
        )
    return [_row_to_dict(r) for r in rows]


async def get_freshness(pool: asyncpg.Pool) -> Dict[str, Any]:
    """Data-freshness snapshot used by /api/ops/freshness."""
    out: Dict[str, Any] = {}
    async with pool.acquire() as conn:
        try:
            out["last_sale_at"] = await conn.fetchval("SELECT max(sold_at) FROM sales_history")
        except Exception:
            out["last_sale_at"] = None
        try:
            out["last_bin_at"] = await conn.fetchval("SELECT max(captured_at) FROM bin_history")
        except Exception:
            out["last_bin_at"] = None
        try:
            out["last_catalog_price_at"] = await conn.fetchval(
                "SELECT max(price_updated_at) FROM fut_players"
            )
        except Exception:
            out["last_catalog_price_at"] = None
        try:
            out["fair_value_computed_at"] = await conn.fetchval(
                "SELECT max(computed_at) FROM fair_value_mv"
            )
        except Exception:
            out["fair_value_computed_at"] = None
    return out
