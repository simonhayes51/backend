# app/services/futgg_recommendation_store.py
"""
Persistence for FUT.GG recommendation snapshots, plus the aggregate
track-record queries built on top of them.

Two responsibilities, deliberately kept in one module because they are
two ends of the same contract - what we wrote down, and what we are
allowed to claim from it:

  * record_recommendation() freezes an evaluation. Nothing it writes is
    ever recomputed from live data. A snapshot row is a historical
    assertion ("at 14:05 we said buy below 88,000 expecting 4.1%"), and
    the moment it starts re-deriving itself against today's market it
    stops being evidence of anything.

  * track_record() aggregates graded outcomes. Every aggregate is gated
    on a minimum sample size and reports that sample size alongside the
    number, because a 100% hit rate over three trades is not a track
    record - it is noise with a percentage sign on it.

Only actionable calls (buy / strong_buy) and watch-with-a-trigger states
are worth persisting. Recording every evaluation of every card would add
millions of rows a day describing cards nobody was ever advised to touch.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence

from app.services.futgg_config import ENGINE_CONFIG
from app.services.futgg_intelligence import CardIntelligence

log = logging.getLogger("futgg_recommendation_store")

# Signals worth freezing. A 'hold'/'insufficient_data' evaluation carries
# no assertion to grade later.
PERSISTED_SIGNALS = frozenset({"buy", "strong_buy", "watch"})

# Aggregates below this many graded outcomes are reported as
# has_enough_data=False with null metrics, never as a number.
MIN_SAMPLE_FOR_RATE = 20


def _num(value: Optional[Decimal]) -> Optional[float]:
    return float(value) if value is not None else None


_INSERT = """
INSERT INTO futgg_recommendation_snapshots (
    source_card_id, evaluated_at,
    current_bin, bin_captured_at, price_age_minutes,
    sales_median, sales_trimmed_mean, sales_count,
    sales_window_earliest_at, sales_window_latest_at,
    sales_window_span_minutes, sales_dispersion_ratio, latest_sale_price,
    price_tier, rating, rarity,
    fair_value, theoretical_max_buy, recommended_buy_max,
    current_executable_buy, break_even_price, recommended_sell_target,
    buy_below, expected_profit_after_tax, expected_roi,
    confidence_score, liquidity_score, risk_level, signal, status,
    trend_state, trend_features,
    reason_codes, blocking_codes, reasons,
    engine_version, trend_version, engine_config,
    expires_at, expiry_minutes
) VALUES (
    $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,
    $21,$22,$23,$24,$25,$26,$27,$28,$29,$30,$31,$32,$33,$34,$35,$36,$37,$38,$39,$40
)
-- Must match futgg_rec_snap_card_minute_uniq in migration 040 exactly,
-- including the AT TIME ZONE 'UTC' (which is there because date_trunc
-- over a TIMESTAMPTZ is STABLE, not IMMUTABLE, and so cannot be
-- indexed). Postgres infers the target index by matching this
-- expression; any divergence and it reports that no matching unique
-- constraint exists.
ON CONFLICT (source_card_id, date_trunc('minute', evaluated_at AT TIME ZONE 'UTC')) DO NOTHING
RETURNING id
"""


# Write failures here are almost always systemic rather than per-card -
# a missing table, a schema behind the code - so the interesting signal
# is "the writer is failing and why", not one line per card. Logging a
# full traceback per card turned a single missing table into thousands of
# messages a minute: Railway's 500/sec replica limit dropped 8,658 of
# them in one burst, which discarded the migration errors we needed to
# diagnose it. Noise this dense is not merely untidy, it destroys
# evidence.
#
# So: first failure of a given class logs once with the traceback, and
# subsequent ones are counted and summarised at most once a minute.
_write_failure_counts: Dict[str, int] = {}
_write_failure_last_log: Dict[str, float] = {}
_WRITE_FAILURE_LOG_INTERVAL_SECONDS = 60.0


def _log_write_failure(exc: BaseException, card_id: Any) -> None:
    key = type(exc).__name__
    now = time.monotonic()
    count = _write_failure_counts.get(key, 0) + 1
    _write_failure_counts[key] = count
    last = _write_failure_last_log.get(key)

    if last is None:
        _write_failure_last_log[key] = now
        log.warning(
            "failed to record recommendation (card_id=%s, %s) - further "
            "failures of this type will be summarised, not logged individually",
            card_id, key, exc_info=True,
        )
        return

    if now - last >= _WRITE_FAILURE_LOG_INTERVAL_SECONDS:
        _write_failure_last_log[key] = now
        log.warning(
            "recommendation writer still failing: %s x%d since start "
            "(most recent card_id=%s)", key, count, card_id,
        )


async def record_recommendation(
    pool, snapshot: Dict[str, Any], ci: CardIntelligence,
) -> Optional[int]:
    """Freeze one evaluation. Returns the new row id, or None if an
    evaluation for this card+minute already existed (the unique index
    makes retries safe without a wrapping transaction).

    Best-effort by contract: a failure here must never break the request
    that produced the recommendation. Losing an audit row is bad; failing
    a user's page load to record one is worse.
    """
    if ci.signal not in PERSISTED_SIGNALS:
        return None
    try:
        async with pool.acquire() as conn:
            return await conn.fetchval(
                _INSERT,
                ci.card_id, ci.evaluated_at or datetime.now(timezone.utc),
                snapshot.get("current_bin"), snapshot.get("bin_captured_at"),
                ci.price_age_minutes,
                snapshot.get("sales_median"), snapshot.get("sales_trimmed_mean"),
                ci.sales_sample_size,
                snapshot.get("sales_window_earliest_at"),
                snapshot.get("sales_window_latest_at"),
                ci.sales_window_span_minutes, snapshot.get("sales_dispersion_ratio"),
                snapshot.get("latest_sale_price"),
                snapshot.get("price_tier"), snapshot.get("rating"), snapshot.get("rarity"),
                _num(ci.fair_value), _num(ci.theoretical_max_buy),
                _num(ci.recommended_buy_max), _num(ci.current_executable_buy),
                _num(ci.break_even_price), _num(ci.recommended_sell_target),
                _num(ci.buy_below), _num(ci.expected_profit_after_tax),
                _num(ci.expected_roi), ci.confidence_score, ci.liquidity_score,
                ci.risk_level, ci.signal, ci.status,
                ci.trend_state, json.dumps(ci.trend_features or {}),
                list(ci.reason_codes or []), list(ci.blocking_codes or []),
                json.dumps(ci.reasons or []),
                ci.engine_version, ci.trend_version,
                json.dumps(ENGINE_CONFIG.as_dict()),
                ci.expires_at, ci.expiry_minutes,
            )
    except Exception as exc:
        _log_write_failure(exc, ci.card_id)
        return None


async def record_many(pool, pairs: Sequence[Any]) -> int:
    """Freeze a batch of (snapshot, CardIntelligence) pairs. Used by the
    segmented scanner, which produces many evaluations per pass."""
    written = 0
    for snapshot, ci in pairs:
        if await record_recommendation(pool, snapshot, ci) is not None:
            written += 1
    return written


# =============================================================================
# Track record
# =============================================================================

_HEADLINE = """
SELECT
    count(*)                                                      AS total,
    count(*) FILTER (WHERE o.entry_achieved)                      AS entered,
    count(*) FILTER (WHERE o.target_hit)                          AS target_hits,
    count(*) FILTER (WHERE o.entry_achieved AND o.realised_roi > 0) AS profitable,
    avg(o.realised_roi) FILTER (WHERE o.entry_achieved)           AS avg_roi,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY o.realised_roi)
        FILTER (WHERE o.entry_achieved)                           AS median_roi,
    avg(o.minutes_to_target) FILTER (WHERE o.target_hit)          AS avg_minutes_to_target,
    avg(o.max_adverse_excursion) FILTER (WHERE o.entry_achieved)  AS avg_mae,
    avg(o.max_favourable_excursion) FILTER (WHERE o.entry_achieved) AS avg_mfe
FROM futgg_recommendation_outcomes o
JOIN futgg_recommendation_snapshots s ON s.id = o.snapshot_id
WHERE o.horizon = $1
  AND o.outcome_status <> 'insufficient_observations'
  AND s.evaluated_at >= now() - $2::interval
"""


def _breakdown_sql(dimension: str) -> str:
    """Build a grouped track-record query.

    `dimension` is interpolated into SQL, so it is resolved from a fixed
    whitelist by the caller - never from user input.
    """
    return f"""
    SELECT {dimension} AS bucket,
        count(*) AS total,
        count(*) FILTER (WHERE o.entry_achieved) AS entered,
        count(*) FILTER (WHERE o.target_hit) AS target_hits,
        count(*) FILTER (WHERE o.entry_achieved AND o.realised_roi > 0) AS profitable,
        avg(o.realised_roi) FILTER (WHERE o.entry_achieved) AS avg_roi
    FROM futgg_recommendation_outcomes o
    JOIN futgg_recommendation_snapshots s ON s.id = o.snapshot_id
    WHERE o.horizon = $1
      AND o.outcome_status <> 'insufficient_observations'
      AND s.evaluated_at >= now() - $2::interval
    GROUP BY 1
    ORDER BY 1
    """


# Whitelist of groupable dimensions -> SQL expression. Anything not in
# here is rejected rather than interpolated.
BREAKDOWN_DIMENSIONS: Dict[str, str] = {
    "confidence_band": (
        "CASE WHEN s.confidence_score >= 0.8 THEN '0.8-1.0'"
        "     WHEN s.confidence_score >= 0.6 THEN '0.6-0.8'"
        "     WHEN s.confidence_score >= 0.45 THEN '0.45-0.6'"
        "     ELSE 'below-0.45' END"
    ),
    "risk_level": "s.risk_level",
    "price_tier": "COALESCE(s.price_tier, 'unknown')",
    "trend_state": "COALESCE(s.trend_state, 'unknown')",
    "engine_version": "s.engine_version",
    "signal": "s.signal",
}


def _rate(numerator: Optional[int], denominator: Optional[int]) -> Optional[float]:
    if not denominator:
        return None
    return round(100.0 * (numerator or 0) / denominator, 1)


async def track_record(
    pool, *, horizon: str = "24h", window_days: int = 90,
    min_sample: int = MIN_SAMPLE_FOR_RATE,
) -> Dict[str, Any]:
    """Aggregate graded outcomes into the public track record.

    Every rate is accompanied by the sample it rests on, and a bucket
    below `min_sample` reports has_enough_data=False with null metrics
    rather than a number. This is the difference between a track record
    and a marketing claim.
    """
    interval = f"{int(window_days)} days"
    async with pool.acquire() as conn:
        head = await conn.fetchrow(_HEADLINE, horizon, interval)
        breakdowns: Dict[str, List[Dict[str, Any]]] = {}
        for name, expression in BREAKDOWN_DIMENSIONS.items():
            rows = await conn.fetch(_breakdown_sql(expression), horizon, interval)
            breakdowns[name] = [
                {
                    "bucket": row["bucket"],
                    "sample_size": int(row["total"]),
                    "has_enough_data": int(row["total"]) >= min_sample,
                    "entry_rate_pct": _rate(row["entered"], row["total"])
                        if int(row["total"]) >= min_sample else None,
                    "target_hit_rate_pct": _rate(row["target_hits"], row["entered"])
                        if int(row["total"]) >= min_sample else None,
                    "profitable_rate_pct": _rate(row["profitable"], row["entered"])
                        if int(row["total"]) >= min_sample else None,
                    "avg_roi_pct": round(float(row["avg_roi"]) * 100, 2)
                        if row["avg_roi"] is not None and int(row["total"]) >= min_sample else None,
                }
                for row in rows
            ]

        # Reason codes need their own query - the column is an array, so
        # it has to be unnested rather than grouped directly.
        reason_rows = await conn.fetch(
            """
            SELECT code AS bucket,
                count(*) AS total,
                count(*) FILTER (WHERE o.entry_achieved) AS entered,
                count(*) FILTER (WHERE o.target_hit) AS target_hits,
                count(*) FILTER (WHERE o.entry_achieved AND o.realised_roi > 0) AS profitable,
                avg(o.realised_roi) FILTER (WHERE o.entry_achieved) AS avg_roi
            FROM futgg_recommendation_outcomes o
            JOIN futgg_recommendation_snapshots s ON s.id = o.snapshot_id
            CROSS JOIN LATERAL unnest(s.reason_codes) AS code
            WHERE o.horizon = $1
              AND o.outcome_status <> 'insufficient_observations'
              AND s.evaluated_at >= now() - $2::interval
            GROUP BY 1
            ORDER BY 2 DESC
            """,
            horizon, interval,
        )
        breakdowns["reason_code"] = [
            {
                "bucket": row["bucket"],
                "sample_size": int(row["total"]),
                "has_enough_data": int(row["total"]) >= min_sample,
                "entry_rate_pct": _rate(row["entered"], row["total"])
                    if int(row["total"]) >= min_sample else None,
                "target_hit_rate_pct": _rate(row["target_hits"], row["entered"])
                    if int(row["total"]) >= min_sample else None,
                "profitable_rate_pct": _rate(row["profitable"], row["entered"])
                    if int(row["total"]) >= min_sample else None,
                "avg_roi_pct": round(float(row["avg_roi"]) * 100, 2)
                    if row["avg_roi"] is not None and int(row["total"]) >= min_sample else None,
            }
            for row in reason_rows
        ]

    total = int(head["total"] or 0)
    entered = int(head["entered"] or 0)
    enough = total >= min_sample

    return {
        "horizon": horizon,
        "window_days": window_days,
        "min_sample": min_sample,
        "has_enough_data": enough,
        "headline": {
            "total_recommendations": total,
            "entered": entered,
            "entry_rate_pct": _rate(entered, total) if enough else None,
            "target_hit_rate_pct": _rate(head["target_hits"], entered) if enough else None,
            "profitable_rate_pct": _rate(head["profitable"], entered) if enough else None,
            "avg_roi_pct": round(float(head["avg_roi"]) * 100, 2)
                if head["avg_roi"] is not None and enough else None,
            "median_roi_pct": round(float(head["median_roi"]) * 100, 2)
                if head["median_roi"] is not None and enough else None,
            "avg_hold_minutes": int(head["avg_minutes_to_target"])
                if head["avg_minutes_to_target"] is not None and enough else None,
            "avg_max_adverse_excursion_pct": round(float(head["avg_mae"]) * 100, 2)
                if head["avg_mae"] is not None and enough else None,
            "avg_max_favourable_excursion_pct": round(float(head["avg_mfe"]) * 100, 2)
                if head["avg_mfe"] is not None and enough else None,
        },
        "breakdowns": breakdowns,
        "methodology": (
            "Every actionable recommendation is frozen at the moment it is made and graded "
            "later against the live listing series, in chronological order. Entry must occur "
            "before exit - the best price in the window is never used. Recommendations whose "
            "buy price was never reached are reported as 'no entry' and excluded from profit "
            "statistics rather than counted as wins or losses. Percentages are withheld "
            f"entirely below {min_sample} graded outcomes."
        ),
    }
