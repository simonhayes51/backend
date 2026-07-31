# app/services/held_position_refresher.py
#
# Wires recommendation_engine_v2.py's held-position logic
# (_evaluate_held_position / held_purchase_price) to real data. That logic
# has existed since the engine was built but has never had a real caller -
# there was no table recording "user owns this card, paid X, hasn't sold"
# until migration 032 added trades.status='open'. This is that caller:
# one pass over every open trade, re-evaluating it as a held position and
# persisting the live verdict onto the trade row itself (migration 035's
# current_recommendation_status/reasoning/evaluated_at), separate from the
# recommendation_* columns migration 032 added, which snapshot what the
# engine said at purchase time and never change afterward.
#
# trades lives in the app's primary pool (DATABASE_URL), not player_pool
# (PLAYER_DATABASE_URL) - evaluate_card needs a player_pool connection to
# read fair_value_mv/sales_history/bin_history, so this loop bridges both.
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import asyncpg

from app.services.recommendation_engine_v2 import evaluate_card

log = logging.getLogger("held_position_refresher")

REFRESH_LOCK_KEY = 7741010  # distinct from every other REFRESH_LOCK_KEY in this codebase


async def refresh_held_positions(pool: asyncpg.Pool, player_pool: asyncpg.Pool) -> int:
    """One pass over every open trade with a known card_id. Returns the
    number of trades whose live verdict was updated."""
    written = 0
    async with pool.acquire() as conn:
        open_trades = await conn.fetch(
            """
            SELECT trade_id, card_id, buy, platform
            FROM trades
            WHERE status = 'open' AND card_id IS NOT NULL AND buy IS NOT NULL
            """
        )

    for row in open_trades:
        trade_id = row["trade_id"]
        card_id = int(row["card_id"])
        platform = row["platform"] or "ps"
        try:
            async with player_pool.acquire() as eval_conn:
                result = await evaluate_card(
                    eval_conn,
                    card_id,
                    platform=platform,
                    is_held=True,
                    held_purchase_price=int(row["buy"]),
                    requested_by="held_position_refresher",
                )
        except Exception:
            log.exception("held_position_refresher: evaluation failed for trade_id=%s card_id=%s", trade_id, card_id)
            continue

        if result is None:
            # No fair_value_mv row for this card yet - leave the trade's
            # existing verdict alone rather than overwrite it with nothing.
            continue

        status = result.held_decision or result.status
        reasoning = " ".join(result.held_decision_reasons) if result.held_decision_reasons else None

        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE trades
                SET current_recommendation_status = $2,
                    current_recommendation_reasoning = $3,
                    current_evaluated_at = now()
                WHERE trade_id = $1
                """,
                trade_id, status, reasoning,
            )
        written += 1

    return written


async def refresher_loop(pool: asyncpg.Pool, player_pool: asyncpg.Pool, interval_seconds: int = 900) -> None:
    """Background task started from the app lifespan. Same advisory-lock
    pattern as fair_value.refresher_loop / event_impact.refresher_loop."""
    await asyncio.sleep(10)
    while True:
        try:
            async with pool.acquire() as conn:
                got = await conn.fetchval("SELECT pg_try_advisory_lock($1)", REFRESH_LOCK_KEY)
                if got:
                    try:
                        n = await refresh_held_positions(pool, player_pool)
                        if n:
                            log.info("held_position_refresher: updated %d open trades", n)
                    finally:
                        await conn.execute("SELECT pg_advisory_unlock($1)", REFRESH_LOCK_KEY)
        except Exception as e:  # never let the loop die
            log.error("held_position_refresher iteration failed: %s", e)
        await asyncio.sleep(interval_seconds)
