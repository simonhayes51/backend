"""
ML feature snapshot pipeline - Recommendation Engine V1.2 Phase 7.

Runs on its own hourly cadence (independent of recommendation_engine_v2's
refresher, which reacts to fair_value_mv's watermark): for every card
with enough live market data, records a versioned, point-in-time
feature snapshot into ml_feature_snapshots (migration 024), then opens
one label window per horizon (24h/48h/7d) in ml_labels for
ml_label_filler.py to close later once real time has actually passed.

This module ONLY collects data - it never writes to ml_model_registry
or recommendations, and evaluate() is called here purely to derive the
same hard-gate/score fields the live engine would have produced, not to
make or persist a live decision. See recommendation_engine_v2.py's
module docstring and trading_math.py's for why no prediction is ever
fabricated from this data before a model is trained and validated
offline.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

import asyncpg

from app.services import trading_math as tm
from app.services.strategy_config import STRATEGY_POLICIES
from app.services.recommendation_engine_v2 import (
    EvaluationInputs,
    evaluate,
    fetch_bin_observations,
    fetch_market_snapshot,
    fetch_sales_prices,
)

log = logging.getLogger("ml_feature_pipeline")

FEATURE_PIPELINE_VERSION = "fp-v1"
LABEL_POLICY_VERSION = "lp-v1"

# Same liquidity floor recommendation_engine_v2.run_pass_v2 uses to pick
# candidates - a card with no real trading activity has nothing useful
# to snapshot either.
MIN_SALES_24H_FLOOR = 3

# Snapshot lock key namespace: distinct from fair_value.py's
# REFRESH_LOCK_KEY and recommendation_engine_v2's 7741007, so this pass
# never contends with either.
SNAPSHOT_LOCK_KEY = 7741009

# Which strategy's minimum required net ROI defines "success" for a
# label opened at each horizon (see strategy_config.py) - not an
# independently invented number. Only the three strategies with a fixed
# holding period get a label window; low_risk/lazy_buyer/sbc are
# flexible-horizon by design and have no single window to grade against.
HORIZON_STRATEGY = {"24h": "quick_flip", "48h": "swing_trade", "7d": "long_hold"}


async def snapshot_card(
    conn: asyncpg.Connection, card_id: int, platform: str = "ps", as_of: Optional[datetime] = None
) -> Optional[int]:
    """Builds and inserts one ml_feature_snapshots row plus one open
    ml_labels row per horizon. Returns the new snapshot id, or None if
    the card has no fair_value_mv row at all (nothing to snapshot -
    different from a real but gate-failing snapshot, which still gets
    recorded)."""
    as_of = as_of or datetime.now(timezone.utc)

    market_row = await conn.fetchrow(
        "SELECT computed_at FROM fair_value_mv WHERE card_id = $1", card_id
    )
    market = await fetch_market_snapshot(conn, card_id, platform)
    if market is None:
        return None

    sales_24h, sales_7d = await fetch_sales_prices(conn, card_id)
    bin_observations = await fetch_bin_observations(conn, card_id, platform)

    inputs = EvaluationInputs(
        market=market, sales_24h_prices=sales_24h, sales_7d_prices=sales_7d,
        bin_observations=bin_observations,
    )
    result = evaluate(inputs, as_of)
    would_pass_live_gates = result.status != "INSUFFICIENT_DATA"

    if market.entry_price is None:
        eligibility_tier = "INVALID"
    elif would_pass_live_gates:
        eligibility_tier = "LIVE_ELIGIBLE"
    else:
        eligibility_tier = "MODEL_ELIGIBLE"

    fraction_at_or_above_likely = None
    if result.likely_price is not None:
        sample = sales_24h if result.sales_window == "24h" else sales_7d
        fraction_at_or_above_likely = tm.historical_fraction_at_or_above_likely(sample, result.likely_price)

    row = await conn.fetchrow(
        """
        INSERT INTO ml_feature_snapshots (
            card_id, platform, snapshot_at, feature_pipeline_version, source_market_computed_at,
            entry_price, fair_value_24h, fair_value_7d, sales_24h, sales_7d, sales_per_hour_24h,
            volatility_24h, bin_zscore_24h, trend_falling, data_quality_suspect, price_age_minutes,
            break_even_sale_price, conservative_price, likely_price, bullish_price, potential_price,
            conservative_net_roi, likely_net_roi, bullish_net_roi, potential_net_roi,
            historical_fraction_at_or_above_likely,
            score_valuation, score_momentum, score_liquidity, score_risk, score_confidence,
            card_rating, card_position, card_version,
            eligibility_tier, would_pass_live_gates, failed_gate_reasons
        ) VALUES (
            $1,$2,$3,$4,$5, $6,$7,$8,$9,$10,$11, $12,$13,$14,$15,$16,
            $17,$18,$19,$20,$21, $22,$23,$24,$25, $26,
            $27,$28,$29,$30,$31, $32,$33,$34,
            $35,$36,$37::jsonb
        )
        RETURNING id
        """,
        card_id, platform, as_of, FEATURE_PIPELINE_VERSION, market_row["computed_at"] if market_row else None,
        market.entry_price, market.fair_value_24h, market.fair_value_7d, market.sales_24h, market.sales_7d,
        market.sales_per_hour_24h, market.volatility_24h, market.bin_zscore_24h, market.trend_falling,
        market.data_quality_suspect, result.price_age_minutes,
        result.break_even_sale_price, result.conservative_price, result.likely_price, result.bullish_price,
        result.potential_price,
        result.conservative_net_roi, result.likely_net_roi, result.bullish_net_roi, result.potential_net_roi,
        fraction_at_or_above_likely,
        result.score_valuation, result.score_momentum, result.score_liquidity, result.score_risk,
        result.score_confidence,
        market.card_rating, market.card_position, market.card_version,
        eligibility_tier, would_pass_live_gates, json.dumps(result.failed_gate_reasons),
    )
    snapshot_id = int(row["id"])

    # No cost basis, no window to grade a "should I have sold" call
    # against - a label needs an entry price the same way a live BUY
    # evaluation does.
    if market.entry_price is not None:
        for horizon, strategy_name in HORIZON_STRATEGY.items():
            policy = STRATEGY_POLICIES[strategy_name]
            target_price = int(tm.strategy_target_price(market.entry_price, policy.min_likely_net_roi))
            await conn.execute(
                """
                INSERT INTO ml_labels (feature_snapshot_id, horizon, label_policy_version, entry_price, strategy_target_price)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (feature_snapshot_id, horizon, label_policy_version) DO NOTHING
                """,
                snapshot_id, horizon, LABEL_POLICY_VERSION, market.entry_price, target_price,
            )

    return snapshot_id


async def run_snapshot_pass(player_pool: asyncpg.Pool) -> int:
    """One pass over every card with enough liquidity to snapshot.
    Returns the number of snapshots written. Holds a single connection
    for the whole pass (lock through unlock) - pg_advisory_lock is
    session-scoped, so acquiring/releasing on different pooled
    connections would silently fail to unlock (same reasoning as
    fair_value.py's refresh_fair_value_mv)."""
    async with player_pool.acquire() as conn:
        got = await conn.fetchval("SELECT pg_try_advisory_lock($1)", SNAPSHOT_LOCK_KEY)
        if not got:
            return 0  # another instance is already running this hour's pass
        try:
            candidates = await conn.fetch(
                "SELECT card_id FROM fair_value_mv WHERE sales_24h >= $1 AND NOT data_quality_suspect",
                MIN_SALES_24H_FLOOR,
            )
            as_of = datetime.now(timezone.utc)
            written = 0
            for row in candidates:
                try:
                    snap_id = await snapshot_card(conn, row["card_id"], as_of=as_of)
                    if snap_id is not None:
                        written += 1
                except Exception:
                    log.exception("ml_feature_pipeline: snapshot failed for card_id=%s", row["card_id"])
            return written
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", SNAPSHOT_LOCK_KEY)



def _legacy_futbin_enabled() -> bool:
    """The FUTBIN chain is retired - see main.py's lifespan comment.

    fair_value_mv is broken on the current player database and its output
    was not merely absent but actively wrong: a card priced 11,250 on
    FUT.GG was served to the player page as 337,000 with an AVOID
    verdict. Every user-visible surface now reads the FUT.GG layer, so
    this loop would burn CPU and connections producing numbers nothing
    should consume.

    Disabled by default rather than deleted, so restoring it for FC27 is
    a config change.
    """
    import os
    return os.getenv("ENABLE_LEGACY_FUTBIN", "0").strip().lower() in {"1", "true", "yes", "on"}

async def refresher_loop(player_pool: asyncpg.Pool, poll_seconds: int = 3600) -> None:
    """Genuinely time-based (not watermark-reactive like the
    recommendation engine's own loop) - "hourly feature snapshots" means
    hourly regardless of whether the underlying market data changed."""
    if not _legacy_futbin_enabled():
        log_name = __name__
        import logging as _logging
        _logging.getLogger(log_name).info(
            "legacy FUTBIN loop disabled (ENABLE_LEGACY_FUTBIN unset)"
        )
        return

    await asyncio.sleep(20)
    while True:
        try:
            n = await run_snapshot_pass(player_pool)
            log.info("ml_feature_pipeline pass: %d snapshots written", n)
        except Exception as e:  # never let the loop die
            log.error("ml_feature_pipeline refresher iteration failed: %s", e)
        await asyncio.sleep(poll_seconds)
