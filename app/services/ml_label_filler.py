"""
ML label filler - Recommendation Engine V1.2 Phase 7.

Closes ml_labels rows whose horizon window has actually elapsed
(snapshot_at + horizon <= now()) by looking at what really happened in
sales_history/bin_history during that window - never a projection or
estimate. A window can only be closed once real time has passed; there
is no way to backfill "the future" faster than the game's own economy
moves, so this runs on the same hourly cadence as
ml_feature_pipeline.py rather than reacting to any watermark.

Split into a pure computation (compute_label_outcome, fully unit-
testable with synthetic sales) and an I/O layer (_close_due_label,
fill_due_labels) that fetches real rows and persists the result -
mirrors recommendation_engine_v2.py's evaluate()/evaluate_card() split.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Sequence, Tuple

import asyncpg

from app.services import trading_math as tm

log = logging.getLogger("ml_label_filler")

HORIZON_TIMEDELTA = {"24h": timedelta(hours=24), "48h": timedelta(hours=48), "7d": timedelta(days=7)}

LABEL_BATCH_SIZE = 200


@dataclass(frozen=True)
class LabelOutcome:
    realized_sale_price: Optional[int]
    realized_at: Optional[datetime]
    target_reached: bool
    time_to_target_minutes: Optional[int]
    mark_to_market_price: Optional[int]
    mark_to_market_return: Optional[object]  # Decimal | None
    strategy_realized_return: Optional[object]  # Decimal | None
    max_favourable_excursion: Optional[object]  # Decimal | None
    max_adverse_excursion: Optional[object]  # Decimal | None
    no_market_activity_in_window: bool


def compute_label_outcome(
    sales_in_window: Sequence[Tuple[int, datetime]],
    entry_price: Optional[int],
    target_price: Optional[int],
    window_start: datetime,
    last_known_bin: Optional[int] = None,
) -> LabelOutcome:
    """`sales_in_window` must already be filtered to
    [window_start, window_start + horizon) and sorted ascending by
    sold_at - this function does no filtering/DB access itself, only
    arithmetic, so it can be unit tested with plain synthetic data.
    `last_known_bin` is the last BIN observation in-window, used as a
    mark-to-market fallback only when there were zero completed sales."""
    realized_sale_price = None
    realized_at = None
    target_reached = False
    time_to_target_minutes = None
    if target_price is not None:
        for price, sold_at in sales_in_window:
            if price >= target_price:
                realized_sale_price = price
                realized_at = sold_at
                target_reached = True
                time_to_target_minutes = max(0, int((sold_at - window_start).total_seconds() // 60))
                break

    if sales_in_window:
        mark_to_market_price = sales_in_window[-1][0]
    else:
        mark_to_market_price = last_known_bin

    mark_to_market_return = None
    if mark_to_market_price is not None and entry_price:
        mark_to_market_return = tm.net_roi(mark_to_market_price, entry_price)

    strategy_realized_return = None
    if target_reached and entry_price:
        strategy_realized_return = tm.net_roi(realized_sale_price, entry_price)
    elif mark_to_market_return is not None:
        strategy_realized_return = mark_to_market_return

    max_fav = None
    max_adv = None
    if sales_in_window and entry_price:
        rois = [r for r in (tm.net_roi(price, entry_price) for price, _ in sales_in_window) if r is not None]
        if rois:
            max_fav = max(rois)
            max_adv = min(rois)

    no_activity = not sales_in_window and mark_to_market_price is None

    return LabelOutcome(
        realized_sale_price=realized_sale_price,
        realized_at=realized_at,
        target_reached=target_reached,
        time_to_target_minutes=time_to_target_minutes,
        mark_to_market_price=mark_to_market_price,
        mark_to_market_return=mark_to_market_return,
        strategy_realized_return=strategy_realized_return,
        max_favourable_excursion=max_fav,
        max_adverse_excursion=max_adv,
        no_market_activity_in_window=no_activity,
    )


async def _close_due_label(conn: asyncpg.Connection, row) -> None:
    horizon = row["horizon"]
    snapshot_at = row["snapshot_at"]
    window_end = snapshot_at + HORIZON_TIMEDELTA[horizon]
    card_id = row["card_id"]
    platform = row["platform"]

    sales_rows = await conn.fetch(
        """
        SELECT sold_price, sold_at FROM sales_history
        WHERE player_id = $1 AND sold_at >= $2 AND sold_at < $3
        ORDER BY sold_at ASC
        """,
        card_id, snapshot_at, window_end,
    )
    sales_in_window = [(r["sold_price"], r["sold_at"]) for r in sales_rows]

    last_known_bin = None
    if not sales_in_window:
        bin_row = await conn.fetchrow(
            """
            SELECT lowest_bin FROM bin_history
            WHERE player_id = $1 AND platform = $2 AND captured_at >= $3 AND captured_at < $4
            ORDER BY captured_at DESC LIMIT 1
            """,
            card_id, platform, snapshot_at, window_end,
        )
        if bin_row:
            last_known_bin = bin_row["lowest_bin"]

    outcome = compute_label_outcome(
        sales_in_window, row["entry_price"], row["strategy_target_price"], snapshot_at, last_known_bin
    )

    await conn.execute(
        """
        UPDATE ml_labels SET
            realized_sale_price = $1, realized_at = $2, target_reached = $3, time_to_target_minutes = $4,
            mark_to_market_price = $5, mark_to_market_return = $6, strategy_realized_return = $7,
            max_favourable_excursion = $8, max_adverse_excursion = $9,
            no_market_activity_in_window = $10, label_closed_at = now()
        WHERE id = $11
        """,
        outcome.realized_sale_price, outcome.realized_at, outcome.target_reached, outcome.time_to_target_minutes,
        outcome.mark_to_market_price, outcome.mark_to_market_return, outcome.strategy_realized_return,
        outcome.max_favourable_excursion, outcome.max_adverse_excursion,
        outcome.no_market_activity_in_window, row["id"],
    )


async def fill_due_labels(player_pool: asyncpg.Pool, *, batch_size: int = LABEL_BATCH_SIZE) -> int:
    """Closes every ml_labels row whose window has elapsed. Returns the
    number of rows closed. Each label is fetched and closed against a
    freshly acquired connection (no cross-row state, unlike
    run_snapshot_pass's single held connection), so a mid-batch failure
    on one card never blocks the rest."""
    async with player_pool.acquire() as conn:
        due = await conn.fetch(
            """
            SELECT l.id, l.horizon, l.entry_price, l.strategy_target_price,
                   s.card_id, s.platform, s.snapshot_at
            FROM ml_labels l
            JOIN ml_feature_snapshots s ON s.id = l.feature_snapshot_id
            WHERE l.label_closed_at IS NULL
              AND s.snapshot_at + (CASE l.horizon
                    WHEN '24h' THEN interval '24 hours'
                    WHEN '48h' THEN interval '48 hours'
                    WHEN '7d' THEN interval '7 days'
                  END) <= now()
            ORDER BY s.snapshot_at ASC
            LIMIT $1
            """,
            batch_size,
        )

    closed = 0
    for row in due:
        async with player_pool.acquire() as conn:
            try:
                await _close_due_label(conn, row)
                closed += 1
            except Exception:
                log.exception("ml_label_filler: failed to close label id=%s", row["id"])
    return closed


async def refresher_loop(player_pool: asyncpg.Pool, poll_seconds: int = 3600) -> None:
    await asyncio.sleep(40)
    while True:
        try:
            n = await fill_due_labels(player_pool)
            if n:
                log.info("ml_label_filler: closed %d label windows", n)
        except Exception as e:  # never let the loop die
            log.error("ml_label_filler refresher iteration failed: %s", e)
        await asyncio.sleep(poll_seconds)
