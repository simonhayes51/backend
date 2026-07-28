# app/services/analytics_engine.py
"""
Ten scores per card, written to card_scores (migration 020) every pass:
Investment, Risk, Confidence, Recovery Probability, Crash Probability,
Market Regime, Momentum, Supply Pressure, Demand Pressure, Opportunity.

Reuses fair_value.py's exact refresh pattern (advisory lock, self-heal
if the MV is missing) and deal_confidence.py's real 7-factor model for
the Confidence score - no scoring logic is duplicated from either.

This is rule_v1: simple, defensible formulas over real inputs
(fair_value_mv, deal_confidence, event_market_impact), not a trained
model. Every score is honest about what it can and can't know yet -
Recovery/Crash Probability in particular are cohort-prior heuristics
(rating band + trend_falling), not learned from real outcome history,
because card_scores itself has no history to learn from on day one.
That's the same reason engine_version is persisted on every row: once
real data accumulates, a v2 engine's accuracy can be compared against
this one on the same schema.

Market Regime is the one globally-computed score - it writes one row
into the CORE database's market_states table (fixing the confirmed gap
where smart_buy.py's /market-intelligence always read a hardcoded
fallback because nothing ever wrote there), then broadcasts the same
regime as a per-card card_scores row so every card still has a uniform
10-score contract.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import asyncpg

from app.services.deal_confidence import compute_deal_confidence

log = logging.getLogger("analytics_engine")

REFRESH_LOCK_KEY = 7741004  # distinct from fair-value (7741002), migration runner (7741003),
                              # event_impact (7741005)
ENGINE_VERSION = "rule_v1"

SCORE_TYPES = [
    "investment", "risk", "confidence", "recovery_probability", "crash_probability",
    "market_regime", "momentum", "supply_pressure", "demand_pressure", "opportunity",
]

DAILY_SCORE_TYPES = {"recovery_probability", "crash_probability"}


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


# ---------------------------------------------------------------------------
# Partition + MV bootstrap
# ---------------------------------------------------------------------------
async def ensure_card_scores_partitions(pool: asyncpg.Pool, months_ahead: int = 1) -> None:
    """Idempotent - creates the current month's partition plus
    `months_ahead` future ones. Computed from wall-clock time in Python,
    never hardcoded dates in SQL (mirrors ensure_fair_value_mv's
    create-if-missing idiom, applied to partitions instead of a matview)."""
    now = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    async with pool.acquire() as conn:
        for i in range(months_ahead + 1):
            start = _add_months(now, i)
            end = _add_months(now, i + 1)
            part_name = f"card_scores_{start.strftime('%Y_%m')}"
            await conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {part_name} PARTITION OF card_scores
                FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')
                """
            )


def _add_months(dt: datetime, n: int) -> datetime:
    month = dt.month - 1 + n
    year = dt.year + month // 12
    month = month % 12 + 1
    return dt.replace(year=year, month=month)


async def ensure_card_scores_latest_mv(pool: asyncpg.Pool) -> bool:
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_matviews WHERE matviewname = 'card_scores_latest'"
        )
    return bool(exists)


# ---------------------------------------------------------------------------
# Per-card scores
# ---------------------------------------------------------------------------
def compute_investment_score(fv_row: Dict[str, Any]) -> float:
    """Higher = more attractive entry. Rewards a real discount with real
    liquidity to actually exit; a falling knife's apparent discount is
    excluded (matches fair_value's own teaser logic)."""
    if fv_row.get("trend_falling") or fv_row.get("data_quality_suspect"):
        return 0.0
    discount = float(fv_row.get("discount_pct") or 0)
    liquidity = float(fv_row.get("sales_per_hour_24h") or 0)
    liquidity_score = _clamp(liquidity * 15, 0, 30)  # ~2/hr already meaningful
    discount_score = _clamp(50 + discount * 2.5, 0, 70)
    return _clamp(discount_score * 0.7 + liquidity_score)


def compute_risk_score(fv_row: Dict[str, Any]) -> float:
    """Higher = riskier. Volatility (as a % of price, not raw coins - a
    900-coin swing means very different things for a 5k card vs a 500k
    one), illiquidity, and a falling trend all raise risk."""
    price = float(fv_row.get("fair_value_24h") or fv_row.get("current_bin") or 0)
    vol = float(fv_row.get("volatility_24h") or 0)
    vol_pct = (vol / price * 100) if price else 0
    liquidity = float(fv_row.get("sales_per_hour_24h") or 0)
    illiquidity_risk = _clamp(30 - liquidity * 10, 0, 30)
    volatility_risk = _clamp(vol_pct * 3, 0, 50)
    falling_risk = 20.0 if fv_row.get("trend_falling") else 0.0
    return _clamp(volatility_risk + illiquidity_risk + falling_risk)


async def compute_confidence_score(card_id: int) -> float:
    """Thin wrapper - the real math lives in deal_confidence.py, not
    duplicated here."""
    result = await compute_deal_confidence(card_id)
    return float(result.get("score") or 0)


def compute_momentum(fv_row: Dict[str, Any]) -> float:
    """fair_value_mv only holds the latest snapshot per card (no
    time-series read here), so momentum is a proxy from the two signals
    that already encode direction: bin_zscore_24h (is the live price
    unusually high/low vs. the recent norm) and trend_falling (a
    confirmed downward move, not just a snapshot anomaly)."""
    z = float(fv_row.get("bin_zscore_24h") or 0)
    base = _clamp(50 + z * 12)
    if fv_row.get("trend_falling"):
        base = _clamp(base - 30)
    return base


def compute_supply_pressure(fv_row: Dict[str, Any], recent_impacts: List[Dict[str, Any]]) -> float:
    """Baseline from sales throughput (more turnover = more supply
    moving), boosted by any recent reward_supply event_market_impact row
    naming this card (a completed SBC just minted new copies of it)."""
    liquidity = float(fv_row.get("sales_per_hour_24h") or 0)
    base = _clamp(liquidity * 12, 0, 60)
    boost = 25.0 if any(i["relation"] == "reward_supply" for i in recent_impacts) else 0.0
    return _clamp(base + boost)


def compute_demand_pressure(fv_row: Dict[str, Any], recent_impacts: List[Dict[str, Any]]) -> float:
    """Baseline from sales throughput + discount pressure (buyers moving
    on a real discount is demand), boosted by any recent fodder_demand/
    requirement_target impact row naming this card."""
    liquidity = float(fv_row.get("sales_per_hour_24h") or 0)
    discount = float(fv_row.get("discount_pct") or 0)
    base = _clamp(liquidity * 10 + max(0, discount) * 1.5, 0, 60)
    boost = 25.0 if any(i["relation"] in ("fodder_demand", "requirement_target") for i in recent_impacts) else 0.0
    return _clamp(base + boost)


def compute_recovery_probability(fv_row: Dict[str, Any]) -> float:
    """Daily cadence. Cohort-prior heuristic, NOT learned from real
    outcome history yet (card_scores has none to learn from on day one -
    see module docstring). Only meaningful for a card currently trending
    down; a stable/rising card's "recovery" question doesn't apply."""
    if not fv_row.get("trend_falling"):
        return 50.0  # not currently falling - the question doesn't apply; neutral prior
    rating = int(fv_row.get("rating") or 75)
    # Prior: higher-rated cards have historically been more resilient
    # (broader demand base, more use cases) than low-rated fodder, which
    # tends to trend toward a floor rather than "recover" - a coarse,
    # explicitly-labeled starting assumption, not a fitted model.
    rating_prior = _clamp((rating - 75) * 2.5, 10, 70)
    liquidity = float(fv_row.get("sales_per_hour_24h") or 0)
    liquidity_bonus = _clamp(liquidity * 5, 0, 15)
    return _clamp(rating_prior + liquidity_bonus)


def compute_crash_probability(fv_row: Dict[str, Any]) -> float:
    """Daily cadence. Cohort-prior heuristic (see module docstring) -
    a price sitting well above its recent norm (high positive
    bin_zscore) with high volatility is the classic setup for a
    correction; already-falling cards score lower here since the
    correction has already started (that's crash_probability's job
    historically, not presently)."""
    if fv_row.get("trend_falling"):
        return 15.0
    z = float(fv_row.get("bin_zscore_24h") or 0)
    price = float(fv_row.get("fair_value_24h") or fv_row.get("current_bin") or 0)
    vol = float(fv_row.get("volatility_24h") or 0)
    vol_pct = (vol / price * 100) if price else 0
    zscore_risk = _clamp(z * 15, 0, 60) if z > 0 else 0.0
    vol_risk = _clamp(vol_pct * 2, 0, 40)
    return _clamp(zscore_risk + vol_risk)


def compute_opportunity_score(this_pass_scores: Dict[str, float]) -> float:
    """Composed LAST from this pass's already-computed in-memory values
    (never re-queried from card_scores - avoids reading a stale value
    mid-pass). Weighted blend favoring investment/confidence, penalized
    by risk."""
    investment = this_pass_scores.get("investment", 0.0)
    confidence = this_pass_scores.get("confidence", 0.0)
    risk = this_pass_scores.get("risk", 0.0)
    momentum = this_pass_scores.get("momentum", 50.0)
    demand = this_pass_scores.get("demand_pressure", 0.0)
    return _clamp(
        investment * 0.35 + confidence * 0.25 + (100 - risk) * 0.20
        + momentum * 0.10 + demand * 0.10
    )


# ---------------------------------------------------------------------------
# Market Regime - the one globally-computed score
# ---------------------------------------------------------------------------
async def compute_market_regime(core_pool: asyncpg.Pool, player_pool: asyncpg.Pool) -> Dict[str, Any]:
    """Pool-wide aggregate over fair_value_mv, classified into a small
    label set. Writes ONE row into core-DB market_states (fixing the
    confirmed never-written gap smart_buy.py's /market-intelligence
    reads from) and returns the same regime for per-card broadcast."""
    async with player_pool.acquire() as conn:
        stats = await conn.fetchrow(
            """
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE trend_falling) AS falling,
                AVG(discount_pct) FILTER (WHERE NOT data_quality_suspect) AS avg_discount,
                AVG(sales_per_hour_24h) AS avg_liquidity
            FROM fair_value_mv
            """
        )
    total = int(stats["total"] or 0)
    falling_pct = (int(stats["falling"] or 0) / total * 100) if total else 0
    avg_discount = float(stats["avg_discount"] or 0)
    avg_liquidity = float(stats["avg_liquidity"] or 0)

    if falling_pct >= 25:
        state, value = "bearish", _clamp(20 - falling_pct * 0.3, 0, 40)
    elif avg_discount >= 5 and falling_pct < 10:
        state, value = "bullish", _clamp(70 + avg_discount, 60, 100)
    elif avg_liquidity < 0.5:
        state, value = "illiquid", 35.0
    else:
        state, value = "normal", 50.0

    confidence = int(_clamp(50 + min(total, 500) / 10, 50, 95))

    async with core_pool.acquire() as conn:
        import json as _json
        await conn.execute(
            """
            INSERT INTO market_states (platform, state, confidence_score, detected_at, indicators)
            VALUES ('ps', $1, $2, now(), $3)
            """,
            state, confidence,
            _json.dumps({
                "total_cards": total, "falling_pct": round(falling_pct, 1),
                "avg_discount_pct": round(avg_discount, 2), "avg_liquidity": round(avg_liquidity, 2),
            }),
        )

    return {"state": state, "value": value, "confidence": confidence}


# ---------------------------------------------------------------------------
# Pass orchestration
# ---------------------------------------------------------------------------
async def _recent_impacts(conn: asyncpg.Connection, card_id: int, days: int = 14) -> List[Dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT relation FROM event_market_impact
        WHERE card_id = $1 AND computed_at >= now() - ($2 || ' days')::interval
        """,
        card_id, str(days),
    )
    return [dict(r) for r in rows]


async def _score_one_card(
    conn: asyncpg.Connection, fv_row: Dict[str, Any], regime: Dict[str, Any],
    compute_daily: bool,
) -> List[Dict[str, Any]]:
    card_id = fv_row["card_id"]
    impacts = await _recent_impacts(conn, card_id)

    scores: Dict[str, float] = {
        "investment": compute_investment_score(fv_row),
        "risk": compute_risk_score(fv_row),
        "confidence": await compute_confidence_score(card_id),
        "momentum": compute_momentum(fv_row),
        "supply_pressure": compute_supply_pressure(fv_row, impacts),
        "demand_pressure": compute_demand_pressure(fv_row, impacts),
        "market_regime": regime["value"],
    }
    if compute_daily:
        scores["recovery_probability"] = compute_recovery_probability(fv_row)
        scores["crash_probability"] = compute_crash_probability(fv_row)

    scores["opportunity"] = compute_opportunity_score(scores)

    now = datetime.now(timezone.utc)
    return [
        {"card_id": card_id, "score_type": st, "value": round(val, 2), "computed_at": now}
        for st, val in scores.items()
    ]


_last_daily_pass: Optional[datetime] = None


async def run_pass(core_pool: asyncpg.Pool, player_pool: asyncpg.Pool) -> int:
    """One full pass over every card in fair_value_mv. Returns the
    number of score rows written."""
    global _last_daily_pass
    await ensure_card_scores_partitions(player_pool)

    now = datetime.now(timezone.utc)
    compute_daily = _last_daily_pass is None or (now - _last_daily_pass) >= timedelta(hours=20)

    regime = await compute_market_regime(core_pool, player_pool)

    written = 0
    async with player_pool.acquire() as conn:
        fv_rows = await conn.fetch(
            """
            SELECT card_id, rating, fair_value_24h, current_bin, discount_pct,
                   sales_per_hour_24h, volatility_24h, bin_zscore_24h,
                   trend_falling, data_quality_suspect
            FROM fair_value_mv
            """
        )

    for fv_row in fv_rows:
        fv_dict = dict(fv_row)
        async with player_pool.acquire() as conn:
            rows = await _score_one_card(conn, fv_dict, regime, compute_daily)
            async with conn.transaction():
                for r in rows:
                    await conn.execute(
                        """
                        INSERT INTO card_scores (card_id, platform, score_type, value, engine_version, computed_at)
                        VALUES ($1, 'ps', $2, $3, $4, $5)
                        """,
                        r["card_id"], r["score_type"], r["value"], ENGINE_VERSION, r["computed_at"],
                    )
                    written += 1

    if compute_daily:
        _last_daily_pass = now

    async with player_pool.acquire() as conn:
        got = await conn.fetchval("SELECT pg_try_advisory_lock($1)", REFRESH_LOCK_KEY)
        if got:
            try:
                await conn.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY card_scores_latest")
            finally:
                await conn.execute("SELECT pg_advisory_unlock($1)", REFRESH_LOCK_KEY)

    return written


async def refresher_loop(core_pool: asyncpg.Pool, player_pool: asyncpg.Pool, poll_seconds: int = 60) -> None:
    """Self-synchronizing: polls fair_value_mv's own watermark rather
    than being called directly from fair_value.py's refresher_loop -
    zero coupling to that already-shipped, verified file. Only runs a
    pass when fair_value_mv has actually refreshed since the last one."""
    await asyncio.sleep(8)
    last_watermark: Optional[datetime] = None
    while True:
        try:
            async with player_pool.acquire() as conn:
                watermark = await conn.fetchval("SELECT max(computed_at) FROM fair_value_mv")
            if watermark and watermark != last_watermark:
                n = await run_pass(core_pool, player_pool)
                log.info("analytics_engine pass: %d score rows written", n)
                last_watermark = watermark
        except Exception as e:  # never let the loop die
            log.error("analytics_engine refresher iteration failed: %s", e)
        await asyncio.sleep(poll_seconds)
