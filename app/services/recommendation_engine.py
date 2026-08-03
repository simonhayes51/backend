# app/services/recommendation_engine.py
"""
AI Recommendation Engine - a strategy pattern over card_scores_latest +
fair_value_mv + recent market_events, producing the recommendations
table's fixed contract (migration 021). rule_v1 is the first
implementation; a future ml_v1 implements the same protocol with a
trained model instead - swapping RECOMMENDATION_ENGINE_VERSION is the
entire "replace the scoring function later" story, since every row
persists its own engine_version and inputs, making both engines'
real-world accuracy directly comparable via outcome_actual_roi_pct once
backtesting backfills it.

Runs after analytics_engine's pass (self-synchronizing on
card_scores_latest's own watermark, same zero-coupling pattern
analytics_engine itself uses against fair_value_mv) - rule_v1 needs
scores to already exist for the cards it's recommending on.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Protocol

import asyncpg

log = logging.getLogger("recommendation_engine")


def _json_safe(v: Any) -> Any:
    """NUMERIC/DECIMAL columns come back from asyncpg as Decimal, which
    json.dumps can't serialize directly (the same gap players.py's
    get_player route already has to work around for its own NUMERIC
    reads) - convert recursively so `inputs` never fails to write."""
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: _json_safe(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_json_safe(x) for x in v]
    return v

REFRESH_LOCK_KEY = 7741006  # distinct from fair-value/migration-runner/event-impact/analytics-engine
MIN_SALES_24H_FLOOR = 3  # below this, sample size is too thin for a confident call


class RecommendationStrategy(Protocol):
    async def generate(
        self, card_id: int, platform: str, *,
        fv_row: Dict[str, Any], scores: Dict[str, float],
        recent_events: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        ...


def _risk_rating(risk_score: float) -> str:
    if risk_score >= 65:
        return "high"
    if risk_score >= 35:
        return "medium"
    return "low"


class RuleV1Strategy:
    """Blends deal_confidence (via the Confidence score),
    card_scores_latest's Investment/Risk/Opportunity, fair_value_mv's
    discount, and recent market_events tagged to the card."""

    async def generate(
        self, card_id: int, platform: str, *,
        fv_row: Dict[str, Any], scores: Dict[str, float],
        recent_events: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        opportunity = scores.get("opportunity", 0.0)
        investment = scores.get("investment", 0.0)
        risk = scores.get("risk", 0.0)
        confidence = scores.get("confidence", 0.0)
        discount = float(fv_row.get("discount_pct") or 0)

        if fv_row.get("trend_falling"):
            recommendation = "avoid"
        elif opportunity >= 65 and confidence >= 50:
            recommendation = "buy"
        elif risk >= 65 or opportunity <= 25:
            recommendation = "avoid"
        elif opportunity <= 45:
            recommendation = "hold"
        else:
            recommendation = "buy" if discount > 0 else "hold"

        # Expected ROI: the discount itself is the honest expectation
        # (fair value already IS the real recent median - closing that
        # gap is the whole thesis), damped by how confident we are in
        # the read. Never invented beyond what discount_pct says.
        expected_roi_pct = round(discount * (confidence / 100.0), 2) if recommendation == "buy" else None

        # Holding period: higher liquidity -> faster expected exit.
        liquidity = float(fv_row.get("sales_per_hour_24h") or 0)
        if recommendation == "buy":
            holding_period_days = max(1, round(7 / max(liquidity, 0.2)))
        else:
            holding_period_days = None

        drivers: List[Dict[str, Any]] = []
        if discount > 0:
            drivers.append({"factor": "discount_vs_fair_value", "value": discount})
        if liquidity > 0:
            drivers.append({"factor": "liquidity_sales_per_hour", "value": liquidity})
        if fv_row.get("trend_falling"):
            drivers.append({"factor": "trend_falling", "value": True})
        for ev in recent_events:
            drivers.append({"factor": f"market_event_{ev['relation']}", "event_id": ev["event_id"], "title": ev.get("title")})

        reasoning_parts = []
        if recommendation == "buy":
            reasoning_parts.append(f"Trading {discount:.1f}% below its real 24h median with {liquidity:.1f} sales/hour liquidity.")
        elif recommendation == "avoid":
            if fv_row.get("trend_falling"):
                reasoning_parts.append("Price is in a confirmed downward trend, not a discount - avoid catching a falling knife.")
            else:
                reasoning_parts.append(f"Risk score ({risk:.0f}/100) or opportunity score ({opportunity:.0f}/100) doesn't clear the bar.")
        else:
            reasoning_parts.append(f"Opportunity score ({opportunity:.0f}/100) is in the neutral range - no strong edge either way right now.")
        if recent_events:
            reasoning_parts.append(f"{len(recent_events)} recent market event(s) affecting this card.")

        return {
            "recommendation": recommendation,
            "confidence": round(confidence, 2),
            "expected_roi_pct": expected_roi_pct,
            "holding_period_days": holding_period_days,
            "risk_rating": _risk_rating(risk),
            "reasoning": " ".join(reasoning_parts),
            "market_drivers": drivers,
            "similar_events": [
                {"event_id": ev["event_id"], "title": ev.get("title"), "relation": ev["relation"]}
                for ev in recent_events
            ],
        }


STRATEGIES: Dict[str, RecommendationStrategy] = {"rule_v1": RuleV1Strategy()}


def get_strategy() -> RecommendationStrategy:
    import os
    version = os.getenv("RECOMMENDATION_ENGINE_VERSION", "rule_v1")
    return STRATEGIES.get(version, STRATEGIES["rule_v1"])


def _engine_version() -> str:
    import os
    return os.getenv("RECOMMENDATION_ENGINE_VERSION", "rule_v1")


async def _recent_events_for_card(conn: asyncpg.Connection, card_id: int, days: int = 14) -> List[Dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT i.event_id, i.relation, e.title
        FROM event_market_impact i
        JOIN market_events e ON e.id = i.event_id
        WHERE i.card_id = $1 AND i.computed_at >= now() - ($2 || ' days')::interval
        """,
        card_id, str(days),
    )
    return [dict(r) for r in rows]


async def run_pass(core_pool: asyncpg.Pool, player_pool: asyncpg.Pool) -> int:
    """One pass over every card with enough liquidity to recommend on.
    Returns the number of recommendation rows written."""
    strategy = get_strategy()
    engine_version = _engine_version()
    written = 0

    async with player_pool.acquire() as conn:
        candidates = await conn.fetch(
            """
            SELECT card_id, rating, fair_value_24h, current_bin, discount_pct,
                   sales_per_hour_24h, sales_24h, volatility_24h, bin_zscore_24h,
                   trend_falling, data_quality_suspect
            FROM fair_value_mv
            WHERE sales_24h >= $1 AND NOT data_quality_suspect
            """,
            MIN_SALES_24H_FLOOR,
        )

    for row in candidates:
        fv_row = dict(row)
        card_id = fv_row["card_id"]
        async with player_pool.acquire() as conn:
            score_rows = await conn.fetch(
                "SELECT score_type, value FROM card_scores_latest WHERE card_id = $1 AND platform = 'ps'",
                card_id,
            )
            scores = {r["score_type"]: float(r["value"]) for r in score_rows}
            if not scores:
                continue  # analytics_engine hasn't scored this card yet - skip, don't guess

            recent_events = await _recent_events_for_card(conn, card_id)
            result = await strategy.generate(card_id, "ps", fv_row=fv_row, scores=scores, recent_events=recent_events)

            import json as _json
            inputs = {"fair_value": {k: v for k, v in fv_row.items() if k != "card_id"}, "scores": scores}
            await conn.execute(
                """
                INSERT INTO recommendations (
                    card_id, platform, recommendation, confidence, expected_roi_pct,
                    holding_period_days, risk_rating, reasoning, market_drivers,
                    similar_events, engine_version, inputs, computed_at
                ) VALUES ($1,'ps',$2,$3,$4,$5,$6,$7,$8,$9,$10,$11, now())
                """,
                card_id, result["recommendation"], result["confidence"], result["expected_roi_pct"],
                result["holding_period_days"], result["risk_rating"], result["reasoning"],
                _json.dumps(_json_safe(result["market_drivers"])), _json.dumps(_json_safe(result["similar_events"])),
                engine_version, _json.dumps(_json_safe(inputs)),
            )
            written += 1

    async with player_pool.acquire() as conn:
        got = await conn.fetchval("SELECT pg_try_advisory_lock($1)", REFRESH_LOCK_KEY)
        if got:
            try:
                await conn.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY recommendations_latest")
            finally:
                await conn.execute("SELECT pg_advisory_unlock($1)", REFRESH_LOCK_KEY)

    return written



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

async def refresher_loop(core_pool: asyncpg.Pool, player_pool: asyncpg.Pool, poll_seconds: int = 60) -> None:
    """Self-synchronizing on card_scores_latest's watermark - only runs
    a pass when analytics_engine has actually produced fresh scores
    since the last one."""
    if not _legacy_futbin_enabled():
        log_name = __name__
        import logging as _logging
        _logging.getLogger(log_name).info(
            "legacy FUTBIN loop disabled (ENABLE_LEGACY_FUTBIN unset)"
        )
        return

    await asyncio.sleep(12)
    last_watermark: Optional[datetime] = None
    while True:
        try:
            async with player_pool.acquire() as conn:
                watermark = await conn.fetchval("SELECT max(computed_at) FROM card_scores_latest")
            if watermark and watermark != last_watermark:
                n = await run_pass(core_pool, player_pool)
                log.info("recommendation_engine pass: %d recommendations written", n)
                last_watermark = watermark
        except Exception as e:  # never let the loop die
            log.error("recommendation_engine refresher iteration failed: %s", e)
        await asyncio.sleep(poll_seconds)
