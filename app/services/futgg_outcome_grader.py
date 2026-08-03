# app/services/futgg_outcome_grader.py
"""
Grades FUT.GG recommendations against what the market actually did next.

THE RULE THAT MATTERS: NO HINDSIGHT
-----------------------------------
The tempting implementation is "did the price ever reach the target in
the next 24 hours" - take the max of the window and compare. That
produces flattering, meaningless numbers, because it grades against
information nobody had at the time and against an execution nobody could
have achieved.

This module grades chronologically instead:

  1. ENTRY. Walk observations forward from the recommendation. Entry is
     the FIRST observation at or below the executable buy price. If the
     card never traded there, the recommendation is `no_entry` - it
     contributes to the entry rate but to none of the profit statistics.
     Scoring an unbuyable call as either a win or a loss is wrong; it
     simply never happened.

  2. EXIT. Continue forward from the entry timestamp. Exit is the first
     observation at or above the sell target that occurs STRICTLY after
     entry. A target that was hit before you could have bought is not
     your exit.

  3. EXCURSIONS. Maximum favourable and adverse excursion are measured
     over the post-entry window only. What the price did before you owned
     the card is not your gain or your drawdown.

Everything is computed from futgg_bin_history, which is the live-ask
series - the price a user could actually have transacted at. Sales
history is deliberately NOT used for grading: it is approximate by
construction (`approximate_sold_at` is derived from relative age text)
and represents other people's fills, not an execution available to us.

The unrealised cases are labelled honestly rather than being forced into
a win/loss binary: a position that is up but never reached its target is
`profitable_unrealised`, not a win. Only `target_hit` is a completed
round trip.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.services import trading_math as tm
from app.services.futgg_config import GRADER_VERSION

log = logging.getLogger("futgg_outcome_grader")

# Horizon label -> window length.
HORIZONS: Dict[str, timedelta] = {
    "24h": timedelta(hours=24),
    "48h": timedelta(hours=48),
    "7d": timedelta(days=7),
}

# Outcome statuses.
NO_ENTRY = "no_entry"
TARGET_HIT = "target_hit"
PROFITABLE_UNREALISED = "profitable_unrealised"
FLAT = "flat"
LOSS_UNREALISED = "loss_unrealised"
DOWNSIDE_HIT = "downside_hit"
INSUFFICIENT_OBSERVATIONS = "insufficient_observations"

# A position this far below entry at any point post-entry is recorded as
# having hit the downside. Not a stop-loss instruction - the engine never
# tells a user to sell at a loss - purely a measurement of how bad the
# drawdown got, so MAE can be summarised as a rate rather than only as a
# distribution.
DOWNSIDE_THRESHOLD = 0.10

# Below this many post-recommendation observations there is not enough
# chronological evidence to grade honestly. Reported as its own status
# rather than being silently counted as a no-entry, which would bias the
# entry rate down on exactly the illiquid cards that are scraped least.
MIN_OBSERVATIONS_TO_GRADE = 2


@dataclass
class OutcomeGrade:
    horizon: str
    window_start: datetime
    window_end: datetime
    observation_count: int
    entry_achieved: bool = False
    entry_at: Optional[datetime] = None
    entry_price: Optional[int] = None
    exit_achieved: bool = False
    exit_at: Optional[datetime] = None
    realised_sell_price: Optional[int] = None
    net_profit_after_tax: Optional[Decimal] = None
    realised_roi: Optional[Decimal] = None
    max_favourable_excursion: Optional[float] = None
    max_adverse_excursion: Optional[float] = None
    target_hit: bool = False
    downside_hit: bool = False
    minutes_to_target: Optional[int] = None
    outcome_status: str = INSUFFICIENT_OBSERVATIONS
    grader_version: str = GRADER_VERSION

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def grade_recommendation(
    *,
    horizon: str,
    evaluated_at: datetime,
    buy_price: float,
    sell_target: float,
    observations: Sequence[Tuple[float, datetime]],
) -> OutcomeGrade:
    """Grade one recommendation over one horizon.

    `observations` is the chronological (price, captured_at) BIN series.
    It may arrive unsorted; it is sorted here rather than trusted, because
    a mis-ordered series would silently break the entry-before-exit
    guarantee that makes these numbers meaningful.

    Pure: no I/O, fully unit-testable.
    """
    window = HORIZONS[horizon]
    evaluated_at = _as_utc(evaluated_at)
    window_start = evaluated_at
    window_end = evaluated_at + window

    series = sorted(
        (
            (float(price), _as_utc(captured_at))
            for price, captured_at in observations
            if price is not None and captured_at is not None
        ),
        key=lambda p: p[1],
    )
    # Only observations strictly inside the horizon count. An observation
    # at exactly evaluated_at is the state the call was made from, not
    # evidence about what happened next.
    series = [p for p in series if window_start < p[1] <= window_end]

    grade = OutcomeGrade(
        horizon=horizon,
        window_start=window_start,
        window_end=window_end,
        observation_count=len(series),
    )

    if len(series) < MIN_OBSERVATIONS_TO_GRADE:
        grade.outcome_status = INSUFFICIENT_OBSERVATIONS
        return grade

    # ---- 1. Entry: first observation at or below the buy price --------
    entry_index: Optional[int] = None
    for index, (price, captured_at) in enumerate(series):
        if price <= buy_price:
            entry_index = index
            grade.entry_achieved = True
            grade.entry_at = captured_at
            grade.entry_price = int(price)
            break

    if entry_index is None:
        # Never purchasable at the advised price. Not a loss - it simply
        # never became a trade.
        grade.outcome_status = NO_ENTRY
        return grade

    entry_price = float(grade.entry_price)
    post_entry = series[entry_index + 1:]

    if not post_entry:
        # Bought on the final observation of the window - nothing after it
        # to measure. Honest answer is "we cannot say", not "flat".
        grade.outcome_status = INSUFFICIENT_OBSERVATIONS
        return grade

    # ---- 2. Excursions, measured post-entry only ----------------------
    highest = max(price for price, _ in post_entry)
    lowest = min(price for price, _ in post_entry)
    grade.max_favourable_excursion = (highest - entry_price) / entry_price
    grade.max_adverse_excursion = (lowest - entry_price) / entry_price
    grade.downside_hit = grade.max_adverse_excursion <= -DOWNSIDE_THRESHOLD

    # ---- 3. Exit: first post-entry observation at or above target -----
    for price, captured_at in post_entry:
        if price >= sell_target:
            grade.exit_achieved = True
            grade.target_hit = True
            grade.exit_at = captured_at
            grade.realised_sell_price = int(price)
            grade.minutes_to_target = int(
                (captured_at - grade.entry_at).total_seconds() // 60
            )
            break

    if grade.exit_achieved:
        grade.net_profit_after_tax = tm.net_profit(grade.realised_sell_price, entry_price)
        grade.realised_roi = tm.net_roi(grade.realised_sell_price, entry_price)
        grade.outcome_status = TARGET_HIT
        return grade

    # ---- 4. No exit: mark to the final observation, labelled honestly --
    # This is a mark-to-market on an open position, not a realised
    # result, and the status says so. Rolling it into "profitable" or
    # "loss" without that distinction is how a track record starts
    # overstating itself.
    final_price = post_entry[-1][0]
    grade.net_profit_after_tax = tm.net_profit(final_price, entry_price)
    grade.realised_roi = tm.net_roi(final_price, entry_price)

    if grade.downside_hit:
        grade.outcome_status = DOWNSIDE_HIT
    elif grade.realised_roi is not None and grade.realised_roi > Decimal("0.005"):
        grade.outcome_status = PROFITABLE_UNREALISED
    elif grade.realised_roi is not None and grade.realised_roi < Decimal("-0.005"):
        grade.outcome_status = LOSS_UNREALISED
    else:
        grade.outcome_status = FLAT
    return grade


# =============================================================================
# Persistence / batch driver
# =============================================================================

_SELECT_UNGRADED = """
    SELECT s.id, s.source_card_id, s.evaluated_at,
           COALESCE(s.current_executable_buy, s.recommended_buy_max) AS buy_price,
           s.recommended_sell_target
    FROM futgg_recommendation_snapshots s
    WHERE s.signal IN ('buy', 'strong_buy')
      AND s.recommended_sell_target IS NOT NULL
      AND COALESCE(s.current_executable_buy, s.recommended_buy_max) IS NOT NULL
      AND s.evaluated_at <= now() - $1::interval
      AND NOT EXISTS (
          SELECT 1 FROM futgg_recommendation_outcomes o
          WHERE o.snapshot_id = s.id AND o.horizon = $2
      )
    ORDER BY s.evaluated_at
    LIMIT $3
"""

_INSERT_OUTCOME = """
    INSERT INTO futgg_recommendation_outcomes (
        snapshot_id, source_card_id, horizon, window_start, window_end,
        observation_count, entry_achieved, entry_at, entry_price,
        exit_achieved, exit_at, realised_sell_price, net_profit_after_tax,
        realised_roi, max_favourable_excursion, max_adverse_excursion,
        target_hit, downside_hit, minutes_to_target, outcome_status,
        grader_version
    ) VALUES (
        $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21
    )
    ON CONFLICT (snapshot_id, horizon) DO NOTHING
"""


async def grade_pending(pool, *, horizon: str, batch_size: int = 200) -> int:
    """Grade up to `batch_size` recommendations whose horizon has fully
    elapsed. Returns the number graded.

    Only grades once the window has completely passed - grading a 7d
    horizon after 3 days would systematically understate target-hit rates
    on slower cards.
    """
    if horizon not in HORIZONS:
        raise ValueError(f"unknown horizon: {horizon}")
    window = HORIZONS[horizon]

    async with pool.acquire() as conn:
        rows = await conn.fetch(_SELECT_UNGRADED, window, horizon, batch_size)
        if not rows:
            return 0

        graded = 0
        for row in rows:
            observations = await conn.fetch(
                """
                SELECT lowest_bin, captured_at
                FROM futgg_bin_history
                WHERE source_card_id = $1
                  AND captured_at > $2
                  AND captured_at <= $3
                ORDER BY captured_at
                """,
                row["source_card_id"], row["evaluated_at"], row["evaluated_at"] + window,
            )
            grade = grade_recommendation(
                horizon=horizon,
                evaluated_at=row["evaluated_at"],
                buy_price=float(row["buy_price"]),
                sell_target=float(row["recommended_sell_target"]),
                observations=[(o["lowest_bin"], o["captured_at"]) for o in observations],
            )
            await conn.execute(
                _INSERT_OUTCOME,
                row["id"], row["source_card_id"], grade.horizon,
                grade.window_start, grade.window_end, grade.observation_count,
                grade.entry_achieved, grade.entry_at, grade.entry_price,
                grade.exit_achieved, grade.exit_at, grade.realised_sell_price,
                grade.net_profit_after_tax, grade.realised_roi,
                grade.max_favourable_excursion, grade.max_adverse_excursion,
                grade.target_hit, grade.downside_hit, grade.minutes_to_target,
                grade.outcome_status, grade.grader_version,
            )
            graded += 1
        return graded


async def grade_all_horizons(pool, *, batch_size: int = 200) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for horizon in HORIZONS:
        try:
            out[horizon] = await grade_pending(pool, horizon=horizon, batch_size=batch_size)
        except Exception:
            log.warning("grading failed for horizon=%s", horizon, exc_info=True)
            out[horizon] = 0
    return out


async def refresher_loop(pool, poll_seconds: int = 1800) -> None:
    """Periodically grade recommendations whose horizon has fully elapsed.

    Runs on a slow cadence by design: a horizon only closes once, so
    grading more often just re-scans rows that are still open. Each pass
    is bounded by batch_size so a large backlog drains gradually rather
    than in one long transaction.
    """
    import asyncio as _asyncio

    await _asyncio.sleep(120)
    while True:
        try:
            graded = await grade_all_horizons(pool)
            if any(graded.values()):
                log.info("outcome grading: %s", graded)
        except Exception:
            log.warning("outcome grading pass failed", exc_info=True)
        await _asyncio.sleep(poll_seconds)
