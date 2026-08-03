"""
Recommendation Engine V1.2 - replaces RuleV1Strategy
(app/services/recommendation_engine.py) as the engine that actually
decides BUY/WAIT/SELL/AVOID/INSUFFICIENT_DATA and writes the
`recommendations` table's V1.2 columns (migration 024).

Deliberately split into two layers, per the working rules this was built
against:
  - `evaluate()` is pure: given already-fetched market data, it returns
    a decision. No I/O, fully unit-testable with synthetic inputs.
  - `evaluate_card()` does the I/O: fetches real data from Postgres,
    calls evaluate(), persists the result, returns it.

recommendation_engine.py (rule_v1) is left in place and still runs - it
is NOT deleted by this file. Swapping the live engine over to rule_v1_2
is an operational decision (env var / feature flag), not something this
module does unilaterally, per "do not require a destructive cutover."

Held-position note: there is no table in this schema that stores "user
currently owns this card, paid X, hasn't sold it yet" - `trades` and
`fut_trades` (migration 010) both require both buy AND sell prices
(closed-trade journals only). So held_purchase_price is NEVER looked up
from the database here - it only exists when a caller explicitly passes
it into evaluate_card()/evaluate() (e.g. a future "what did you pay for
this" form field). Without it, is_held evaluations correctly return
INSUFFICIENT_DATA / MISSING_HELD_COST_BASIS rather than guessing.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple

import asyncpg

from app.services import trading_math as tm
from app.services.strategy_config import (
    LAZY_BUYER_MIN_DISCOUNT_VS_FAIR_VALUE,
    LAZY_BUYER_MIN_DISCOUNT_VS_LIKELY,
    LAZY_BUYER_MIN_LIQUIDITY,
    LAZY_BUYER_MIN_NET_ROI,
    MAX_ACCEPTABLE_STALENESS_MINUTES,
    MIN_DECISION_CONFIDENCE,
    STRATEGY_POLICIES,
    StrategyPolicy,
)

log = logging.getLogger("recommendation_engine_v2")

ENGINE_VERSION = "rule-v1.2"


# =============================================================================
# Input data contracts
# =============================================================================

@dataclass(frozen=True)
class MarketSnapshot:
    card_id: int
    platform: str
    entry_price: Optional[int]
    price_captured_at: Optional[datetime]
    fair_value_24h: Optional[int]
    fair_value_7d: Optional[int]
    sales_24h: Optional[int]
    sales_7d: Optional[int]
    sales_per_hour_24h: Optional[Decimal]
    volatility_24h: Optional[int]
    bin_zscore_24h: Optional[Decimal]
    trend_falling: bool
    data_quality_suspect: bool
    card_rating: Optional[int] = None
    card_position: Optional[str] = None
    card_version: Optional[str] = None


@dataclass(frozen=True)
class EvaluationInputs:
    market: MarketSnapshot
    sales_24h_prices: Sequence[int] = field(default_factory=list)
    sales_7d_prices: Sequence[int] = field(default_factory=list)
    bin_observations: Sequence[Tuple[datetime, int]] = field(default_factory=list)
    sbc_relevant: Optional[bool] = None   # None = unavailable/unknown, never fabricated
    is_held: bool = False
    held_purchase_price: Optional[int] = None
    requested_strategy: Optional[str] = None
    requested_by: str = "scheduled"


@dataclass(frozen=True)
class StrategyResult:
    qualified: bool
    reasons: List[str]


@dataclass
class EvaluationResult:
    card_id: int
    platform: str
    evaluated_at: datetime
    engine_version: str
    champion_source: str
    status: str  # BUY|WAIT|SELL|AVOID|INSUFFICIENT_DATA
    failed_gate_reasons: List[str]
    qualified_strategies: List[str]
    strategy_results: Dict[str, StrategyResult]

    entry_price: Optional[int]
    break_even_sale_price: Optional[int]

    conservative_price: Optional[int]
    likely_price: Optional[int]
    bullish_price: Optional[int]
    potential_price: Optional[int]

    conservative_net_roi: Optional[Decimal]
    likely_net_roi: Optional[Decimal]
    bullish_net_roi: Optional[Decimal]
    potential_net_roi: Optional[Decimal]

    score_valuation: Optional[float]
    score_momentum: Optional[float]
    score_liquidity: Optional[float]
    score_risk: Optional[float]
    score_confidence: Optional[float]

    sales_sample_size: Optional[int]
    sales_window: Optional[str]
    price_age_minutes: Optional[int]

    # Held-position fields (None for a fresh-buy evaluation).
    held_decision: Optional[str] = None
    held_decision_reasons: List[str] = field(default_factory=list)
    purchase_price: Optional[int] = None
    current_exit_net_profit: Optional[Decimal] = None
    current_exit_net_roi: Optional[Decimal] = None
    likely_hold_net_profit: Optional[Decimal] = None
    likely_hold_net_roi: Optional[Decimal] = None
    incremental_hold_value: Optional[Decimal] = None

    model_version: Optional[str] = None
    is_held: bool = False
    requested_by: str = "scheduled"


# =============================================================================
# Hard gates
# =============================================================================

def _price_age_minutes(price_captured_at: Optional[datetime], as_of: datetime) -> Optional[int]:
    if price_captured_at is None:
        return None
    delta = as_of - price_captured_at
    return max(0, int(delta.total_seconds() // 60))


def run_hard_gates(inputs: EvaluationInputs, as_of: datetime) -> List[str]:
    """Returns ALL failed reason codes (not just the first)."""
    m = inputs.market
    reasons: List[str] = []

    if m.entry_price is None:
        reasons.append("MISSING_PRICE")
    if m.data_quality_suspect:
        reasons.append("SUSPECT_DATA")

    age = _price_age_minutes(m.price_captured_at, as_of)
    if age is None or age > MAX_ACCEPTABLE_STALENESS_MINUTES:
        reasons.append("STALE_PRICE")

    if m.fair_value_24h is None or m.fair_value_7d is None:
        reasons.append("MISSING_FAIR_VALUE")

    if m.bin_zscore_24h is None:
        reasons.append("MISSING_ZSCORE")

    scenario = tm.scenario_prices(inputs.sales_24h_prices, inputs.sales_7d_prices, m.fair_value_24h, m.fair_value_7d)
    if scenario is None:
        reasons.append("INSUFFICIENT_COMPLETED_SALES")

    momentum = tm.momentum_score(inputs.bin_observations, m.entry_price, m.trend_falling, as_of=as_of)
    if momentum is None:
        reasons.append("INSUFFICIENT_BIN_HISTORY")

    risk = tm.risk_score(
        [p for _, p in sorted(inputs.bin_observations, key=lambda pair: pair[0])],
        m.entry_price,
        m.volatility_24h,
        age,
    )
    if risk is None:
        reasons.append("MISSING_VOLATILITY")

    confidence = tm.confidence_score(m.sales_24h, m.sales_7d, age, m.bin_zscore_24h)
    if confidence is None:
        reasons.append("LOW_CONFIDENCE")
    elif confidence < MIN_DECISION_CONFIDENCE:
        reasons.append("LOW_CONFIDENCE")

    if inputs.is_held and inputs.held_purchase_price is None:
        reasons.append("MISSING_HELD_COST_BASIS")

    return reasons


# =============================================================================
# Strategy evaluation
# =============================================================================

def _evaluate_policy(
    policy: StrategyPolicy,
    *,
    likely_net_roi: Decimal,
    conservative_net_roi: Decimal,
    liquidity: Optional[float],
    confidence: Optional[float],
    risk: Optional[float],
    momentum: Optional[float],
) -> StrategyResult:
    reasons: List[str] = []

    if likely_net_roi < policy.min_likely_net_roi:
        reasons.append(f"likely_net_roi {likely_net_roi:.4f} below minimum {policy.min_likely_net_roi:.4f}")
    if conservative_net_roi < policy.min_conservative_net_roi:
        reasons.append(
            f"conservative_net_roi {conservative_net_roi:.4f} below minimum {policy.min_conservative_net_roi:.4f}"
        )
    if policy.min_liquidity is not None and (liquidity is None or liquidity < policy.min_liquidity):
        reasons.append(f"liquidity below minimum {policy.min_liquidity}")
    if policy.min_confidence is not None and (confidence is None or confidence < policy.min_confidence):
        reasons.append(f"confidence below minimum {policy.min_confidence}")
    if policy.max_risk is not None and (risk is None or risk > policy.max_risk):
        reasons.append(f"risk above maximum {policy.max_risk}")
    if policy.momentum_floor is not None and (momentum is None or momentum < policy.momentum_floor):
        reasons.append(f"momentum below floor {policy.momentum_floor}")
    if policy.require_positive_momentum and (momentum is None or momentum <= 0):
        reasons.append("momentum must be positive")

    return StrategyResult(qualified=not reasons, reasons=reasons or ["qualifies"])


def _evaluate_lazy_buyer(
    *,
    entry_price: int,
    fair_value_24h: int,
    likely_price: Decimal,
    likely_net_roi: Decimal,
    liquidity: Optional[float],
) -> StrategyResult:
    reasons: List[str] = []

    discount_vs_likely = (likely_price - entry_price) / likely_price if likely_price else Decimal("0")
    discount_vs_fv = (
        Decimal(fair_value_24h - entry_price) / Decimal(fair_value_24h) if fair_value_24h else Decimal("0")
    )

    if discount_vs_likely < LAZY_BUYER_MIN_DISCOUNT_VS_LIKELY:
        reasons.append(f"discount vs likely clearing price {discount_vs_likely:.4f} below minimum")
    if discount_vs_fv < LAZY_BUYER_MIN_DISCOUNT_VS_FAIR_VALUE:
        reasons.append(f"discount vs fair value {discount_vs_fv:.4f} below minimum")
    if liquidity is None or liquidity < LAZY_BUYER_MIN_LIQUIDITY:
        reasons.append(f"liquidity below minimum {LAZY_BUYER_MIN_LIQUIDITY}")
    if likely_net_roi < LAZY_BUYER_MIN_NET_ROI:
        reasons.append(f"likely_net_roi {likely_net_roi:.4f} below lazy-buyer margin {LAZY_BUYER_MIN_NET_ROI}")

    return StrategyResult(qualified=not reasons, reasons=reasons or ["qualifies"])


def _evaluate_sbc(sbc_relevant: Optional[bool]) -> StrategyResult:
    """Never inferred - only ever real market_events(kind='sbc') linkage,
    supplied by the caller. sbc_relevant is None whenever that pipeline
    hasn't determined relevance for this card (NOT the same as False)."""
    if sbc_relevant is None:
        return StrategyResult(qualified=False, reasons=["NO_SBC_DATA"])
    if sbc_relevant is False:
        return StrategyResult(qualified=False, reasons=["card is not relevant to any active SBC"])
    return StrategyResult(qualified=True, reasons=["qualifies"])


# =============================================================================
# Held-position SELL / HOLD
# =============================================================================

MIN_INCREMENTAL_HOLD_VALUE = Decimal("0.02")  # FLAGGED - unverified, documented starting point


def _evaluate_held_position(
    inputs: EvaluationInputs,
    entry_price: int,
    likely_price: Decimal,
    momentum: Optional[float],
    risk: Optional[float],
) -> Tuple[str, List[str], Dict[str, Any]]:
    purchase_price = inputs.held_purchase_price
    assert purchase_price is not None  # caller must have passed the hard gate first

    current_exit_net_profit = tm.net_profit(entry_price, purchase_price)
    current_exit_net_roi = tm.net_roi(entry_price, purchase_price)
    likely_hold_net_profit = tm.net_profit(likely_price, purchase_price)
    likely_hold_net_roi = tm.net_roi(likely_price, purchase_price)

    incremental_hold_value = (
        (likely_hold_net_roi - current_exit_net_roi)
        if likely_hold_net_roi is not None and current_exit_net_roi is not None
        else None
    )

    reasons: List[str] = []
    profitable_now = current_exit_net_profit is not None and current_exit_net_profit > 0
    momentum_negative = momentum is not None and momentum < -0.15
    momentum_strongly_negative = momentum is not None and momentum < -0.30
    outlook_negative = likely_hold_net_roi is not None and likely_hold_net_roi <= 0
    high_risk = risk is not None and risk > 0.6

    if profitable_now and momentum_negative:
        decision = "SELL"
        reasons.append("Position is profitable but momentum has turned materially negative.")
    elif (not profitable_now) and outlook_negative and momentum_strongly_negative:
        decision = "SELL"
        reasons.append("Position is losing, the likely future outlook remains negative, and momentum is strongly negative.")
    elif incremental_hold_value is not None and incremental_hold_value < MIN_INCREMENTAL_HOLD_VALUE and not high_risk:
        decision = "SELL"
        reasons.append(
            f"Expected additional upside from holding ({incremental_hold_value:.4f}) is below the "
            f"minimum ({MIN_INCREMENTAL_HOLD_VALUE}) to justify keeping capital locked up."
        )
    else:
        decision = "HOLD"
        reasons.append("Selling now would underperform the likely future case enough to justify continued exposure.")

    return decision, reasons, {
        "current_exit_net_profit": current_exit_net_profit,
        "current_exit_net_roi": current_exit_net_roi,
        "likely_hold_net_profit": likely_hold_net_profit,
        "likely_hold_net_roi": likely_hold_net_roi,
        "incremental_hold_value": incremental_hold_value,
    }


# =============================================================================
# Prediction providers - deterministic rule engine is the permanent
# champion/fallback; ML is an interface only, disabled until a real
# validated model is registered and promoted (see migration 024's
# ml_model_registry/ml_model_promotions).
# =============================================================================

class MLPrediction(Protocol):
    predicted_mean_roi: Optional[Decimal]
    predicted_median_roi: Optional[Decimal]
    downside_p10: Optional[Decimal]
    upside_p90: Optional[Decimal]
    probability_profitable: Optional[float]
    probability_clears_minimum: Optional[float]
    model_version: str
    feature_pipeline_version: str


class PredictionProvider(Protocol):
    async def predict(self, conn: asyncpg.Connection, card_id: int, platform: str) -> Optional[MLPrediction]:
        ...


class RuleOnlyPredictionProvider:
    """The only provider currently wired in - always returns None. This
    is not a placeholder for "no prediction yet", it is the intentional
    permanent behaviour until champion resolution (see
    resolve_champion_source below) finds a real CHAMPION row."""

    async def predict(self, conn: asyncpg.Connection, card_id: int, platform: str) -> Optional[MLPrediction]:
        return None


async def resolve_champion_source(conn: asyncpg.Connection, horizon: str = "24h") -> Tuple[str, Optional[str]]:
    """Returns (champion_source, model_version). Reads
    ml_model_registry for a CHAMPION row for `horizon` - if none exists
    (true today; nothing in this codebase promotes a model), returns
    ("rule_engine", None) unconditionally. This is the ONLY place that
    may decide ML gets to drive a live decision, and it can never do so
    without a real, audited CHAMPION row."""
    row = await conn.fetchrow(
        "SELECT model_version FROM ml_model_registry WHERE horizon = $1 AND status = 'CHAMPION'",
        horizon,
    )
    if row is None:
        return "rule_engine", None
    return "ml_model", row["model_version"]


# =============================================================================
# Pure decision logic - no I/O, fully unit-testable.
# =============================================================================

def evaluate(inputs: EvaluationInputs, as_of: datetime, champion_source: str = "rule_engine", model_version: Optional[str] = None) -> EvaluationResult:
    m = inputs.market
    failed_gates = run_hard_gates(inputs, as_of)

    if failed_gates:
        return EvaluationResult(
            card_id=m.card_id, platform=m.platform, evaluated_at=as_of, engine_version=ENGINE_VERSION,
            champion_source=champion_source, status="INSUFFICIENT_DATA", failed_gate_reasons=failed_gates,
            qualified_strategies=[], strategy_results={},
            entry_price=m.entry_price, break_even_sale_price=None,
            conservative_price=None, likely_price=None, bullish_price=None, potential_price=None,
            conservative_net_roi=None, likely_net_roi=None, bullish_net_roi=None, potential_net_roi=None,
            score_valuation=None, score_momentum=None, score_liquidity=None, score_risk=None, score_confidence=None,
            sales_sample_size=None, sales_window=None,
            price_age_minutes=_price_age_minutes(m.price_captured_at, as_of),
            model_version=model_version, is_held=inputs.is_held, requested_by=inputs.requested_by,
        )

    entry_price = m.entry_price
    assert entry_price is not None  # hard gate already guaranteed this

    scenario = tm.scenario_prices(inputs.sales_24h_prices, inputs.sales_7d_prices, m.fair_value_24h, m.fair_value_7d)
    assert scenario is not None

    break_even = tm.break_even_sale_price(entry_price)
    conservative_roi = tm.net_roi(scenario.conservative_price, entry_price)
    likely_roi = tm.net_roi(scenario.likely_price, entry_price)
    bullish_roi = tm.net_roi(scenario.bullish_price, entry_price)
    potential_roi = tm.net_roi(scenario.potential_price, entry_price)
    assert conservative_roi is not None and likely_roi is not None and bullish_roi is not None and potential_roi is not None

    age = _price_age_minutes(m.price_captured_at, as_of)
    ordered_bins = sorted(inputs.bin_observations, key=lambda pair: pair[0])

    valuation = tm.valuation_score(m.fair_value_24h, entry_price, scenario.likely_price, m.bin_zscore_24h)
    momentum = tm.momentum_score(inputs.bin_observations, entry_price, m.trend_falling, as_of=as_of)
    liquidity = tm.liquidity_score(m.sales_per_hour_24h, m.sales_24h, m.sales_7d)
    risk = tm.risk_score([p for _, p in ordered_bins], entry_price, m.volatility_24h, age)
    confidence = tm.confidence_score(m.sales_24h, m.sales_7d, age, m.bin_zscore_24h)

    strategy_results: Dict[str, StrategyResult] = {}
    for name, policy in STRATEGY_POLICIES.items():
        strategy_results[name] = _evaluate_policy(
            policy,
            likely_net_roi=likely_roi, conservative_net_roi=conservative_roi,
            liquidity=liquidity, confidence=confidence, risk=risk, momentum=momentum,
        )
    strategy_results["lazy_buyer"] = _evaluate_lazy_buyer(
        entry_price=entry_price, fair_value_24h=m.fair_value_24h, likely_price=scenario.likely_price,
        likely_net_roi=likely_roi, liquidity=liquidity,
    )
    strategy_results["sbc"] = _evaluate_sbc(inputs.sbc_relevant)

    qualified = [name for name, result in strategy_results.items() if result.qualified]

    if likely_roi <= 0:
        # The likely-case scenario itself doesn't clear tax - not merely
        # "no strategy happens to qualify" (WAIT), a genuinely bad trade.
        status = "AVOID"
    elif qualified:
        status = "BUY"
    else:
        status = "WAIT"

    result = EvaluationResult(
        card_id=m.card_id, platform=m.platform, evaluated_at=as_of, engine_version=ENGINE_VERSION,
        champion_source=champion_source, status=status, failed_gate_reasons=[],
        qualified_strategies=qualified, strategy_results=strategy_results,
        entry_price=entry_price, break_even_sale_price=int(break_even),
        conservative_price=int(scenario.conservative_price), likely_price=int(scenario.likely_price),
        bullish_price=int(scenario.bullish_price), potential_price=int(scenario.potential_price),
        conservative_net_roi=conservative_roi, likely_net_roi=likely_roi,
        bullish_net_roi=bullish_roi, potential_net_roi=potential_roi,
        score_valuation=valuation, score_momentum=momentum, score_liquidity=liquidity,
        score_risk=risk, score_confidence=confidence,
        sales_sample_size=scenario.sales_sample_size, sales_window=scenario.sales_window,
        price_age_minutes=age, model_version=model_version,
        is_held=inputs.is_held, requested_by=inputs.requested_by,
    )

    if inputs.is_held and inputs.held_purchase_price is not None:
        held_decision, held_reasons, held_fields = _evaluate_held_position(
            inputs, entry_price, scenario.likely_price, momentum, risk
        )
        result.held_decision = held_decision
        result.held_decision_reasons = held_reasons
        result.purchase_price = inputs.held_purchase_price
        result.current_exit_net_profit = held_fields["current_exit_net_profit"]
        result.current_exit_net_roi = held_fields["current_exit_net_roi"]
        result.likely_hold_net_profit = held_fields["likely_hold_net_profit"]
        result.likely_hold_net_roi = held_fields["likely_hold_net_roi"]
        result.incremental_hold_value = held_fields["incremental_hold_value"]

    return result


# =============================================================================
# I/O layer - fetch real data, call evaluate(), persist.
# =============================================================================

async def fetch_market_snapshot(conn: asyncpg.Connection, card_id: int, platform: str = "ps") -> Optional[MarketSnapshot]:
    row = await conn.fetchrow(
        """
        SELECT card_id, rating, fair_value_24h, fair_value_7d, current_bin, bin_captured_at,
               sales_24h, sales_7d, sales_per_hour_24h, volatility_24h, bin_zscore_24h,
               trend_falling, data_quality_suspect, position, version
        FROM fair_value_mv
        WHERE card_id = $1
        """,
        card_id,
    )
    if row is None:
        return None
    return MarketSnapshot(
        card_id=row["card_id"], platform=platform,
        entry_price=row["current_bin"], price_captured_at=row["bin_captured_at"],
        fair_value_24h=row["fair_value_24h"], fair_value_7d=row["fair_value_7d"],
        sales_24h=row["sales_24h"], sales_7d=row["sales_7d"],
        sales_per_hour_24h=row["sales_per_hour_24h"], volatility_24h=row["volatility_24h"],
        bin_zscore_24h=row["bin_zscore_24h"], trend_falling=bool(row["trend_falling"]),
        data_quality_suspect=bool(row["data_quality_suspect"]),
        card_rating=row["rating"], card_position=row["position"], card_version=row["version"],
    )


async def fetch_sales_prices(conn: asyncpg.Connection, card_id: int) -> Tuple[List[int], List[int]]:
    rows = await conn.fetch(
        """
        SELECT sold_price, sold_at
        FROM sales_history
        WHERE player_id = $1 AND sold_at >= now() - interval '7 days'
        """,
        card_id,
    )
    sales_24h = [r["sold_price"] for r in rows if (datetime.now(timezone.utc) - r["sold_at"]).total_seconds() <= 86400]
    sales_7d = [r["sold_price"] for r in rows]
    return sales_24h, sales_7d


async def fetch_bin_observations(conn: asyncpg.Connection, card_id: int, platform: str = "ps") -> List[Tuple[datetime, int]]:
    rows = await conn.fetch(
        """
        SELECT captured_at, lowest_bin
        FROM bin_history
        WHERE player_id = $1 AND platform = $2 AND captured_at >= now() - interval '48 hours'
              AND lowest_bin IS NOT NULL
        ORDER BY captured_at ASC
        """,
        card_id, platform,
    )
    return [(r["captured_at"], r["lowest_bin"]) for r in rows]


async def evaluate_card(
    conn: asyncpg.Connection,
    card_id: int,
    *,
    platform: str = "ps",
    is_held: bool = False,
    held_purchase_price: Optional[int] = None,
    requested_strategy: Optional[str] = None,
    requested_by: str = "scheduled",
    sbc_relevant: Optional[bool] = None,
) -> Optional[EvaluationResult]:
    """Full flow: fetch -> evaluate -> persist -> return. Returns None
    only when the card has no fair_value_mv row at all (never scored -
    not the same as INSUFFICIENT_DATA, which is a real evaluated result
    that just failed hard gates)."""

    market = await fetch_market_snapshot(conn, card_id, platform)
    if market is None:
        return None

    sales_24h, sales_7d = await fetch_sales_prices(conn, card_id)
    bin_observations = await fetch_bin_observations(conn, card_id, platform)

    inputs = EvaluationInputs(
        market=market, sales_24h_prices=sales_24h, sales_7d_prices=sales_7d,
        bin_observations=bin_observations, sbc_relevant=sbc_relevant,
        is_held=is_held, held_purchase_price=held_purchase_price,
        requested_strategy=requested_strategy, requested_by=requested_by,
    )

    champion_source, model_version = await resolve_champion_source(conn)
    as_of = datetime.now(timezone.utc)
    result = evaluate(inputs, as_of, champion_source=champion_source, model_version=model_version)

    await _persist(conn, result)
    return result


def _legacy_recommendation(status: str, held_decision: Optional[str]) -> str:
    """Derives the deprecated lowercase recommendation column from the
    correct V1.2 status, rather than an independently-computed value -
    never writes a second, possibly-inconsistent opinion."""
    if held_decision:
        return held_decision.lower()
    return {"BUY": "buy", "WAIT": "hold", "SELL": "sell", "AVOID": "avoid", "INSUFFICIENT_DATA": "hold"}.get(status, "hold")


async def _persist(conn: asyncpg.Connection, r: EvaluationResult) -> int:
    import json

    strategy_results_json = {
        name: {"qualified": res.qualified, "reasons": res.reasons} for name, res in r.strategy_results.items()
    }
    legacy_recommendation = _legacy_recommendation(r.status, r.held_decision)
    legacy_expected_roi_pct = float(r.likely_net_roi * 100) if r.likely_net_roi is not None else None
    legacy_confidence = float((r.score_confidence or 0) * 100)

    row = await conn.fetchrow(
        """
        INSERT INTO recommendations (
            card_id, platform, recommendation, confidence, expected_roi_pct,
            engine_version, inputs, status, entry_price, break_even_sale_price,
            conservative_price, likely_price, bullish_price, potential_price,
            conservative_net_roi, likely_net_roi, bullish_net_roi, potential_net_roi,
            expected_net_roi, expected_net_roi_source,
            score_valuation, score_momentum, score_liquidity, score_risk, score_confidence,
            qualified_strategies, strategy_results, failed_gate_reasons,
            sales_sample_size, sales_window, price_age_minutes,
            is_held, purchase_price, held_decision, held_decision_reasons, incremental_hold_value,
            requested_by, champion_source, model_version, computed_at
        ) VALUES (
            $1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,
            $21,$22,$23,$24,$25,$26::jsonb,$27::jsonb,$28::jsonb,$29,$30,$31,
            $32,$33,$34,$35::jsonb,$36,$37,$38,$39, now()
        )
        RETURNING id
        """,
        r.card_id, r.platform, legacy_recommendation, legacy_confidence, legacy_expected_roi_pct,
        r.engine_version, json.dumps({}), r.status, r.entry_price, r.break_even_sale_price,
        r.conservative_price, r.likely_price, r.bullish_price, r.potential_price,
        r.conservative_net_roi, r.likely_net_roi, r.bullish_net_roi, r.potential_net_roi,
        None, "unavailable_until_validated_model",
        r.score_valuation, r.score_momentum, r.score_liquidity, r.score_risk, r.score_confidence,
        json.dumps(r.qualified_strategies), json.dumps(strategy_results_json), json.dumps(r.failed_gate_reasons),
        r.sales_sample_size, r.sales_window, r.price_age_minutes,
        r.is_held, r.purchase_price, r.held_decision, json.dumps(r.held_decision_reasons),
        r.incremental_hold_value, r.requested_by, r.champion_source, r.model_version,
    )
    return int(row["id"])


# =============================================================================
# Batch runner + self-synchronizing refresher loop - mirrors
# recommendation_engine.py's run_pass()/refresher_loop() shape so this
# can be wired into main.py as a drop-in alternative (selected by the
# RECOMMENDATION_ENGINE_VERSION env var, not a destructive replacement).
# Unlike rule_v1, this engine computes its own scores directly from
# fair_value_mv/sales_history/bin_history rather than depending on
# analytics_engine's card_scores_latest, so it self-synchronizes on
# fair_value_mv's own computed_at watermark instead.
# =============================================================================

MIN_SALES_24H_FLOOR = 3  # same floor as recommendation_engine.py's rule_v1


async def run_pass_v2(player_pool: asyncpg.Pool, *, requested_by: str = "scheduled") -> int:
    """One pass over every card with enough liquidity to evaluate.
    Returns the number of evaluations written."""
    written = 0
    async with player_pool.acquire() as conn:
        candidates = await conn.fetch(
            """
            SELECT card_id
            FROM fair_value_mv
            WHERE sales_24h >= $1 AND NOT data_quality_suspect
            """,
            MIN_SALES_24H_FLOOR,
        )

    for row in candidates:
        card_id = row["card_id"]
        async with player_pool.acquire() as conn:
            try:
                result = await evaluate_card(conn, card_id, requested_by=requested_by)
                if result is not None:
                    written += 1
            except Exception:
                log.exception("recommendation_engine_v2: evaluation failed for card_id=%s", card_id)

    async with player_pool.acquire() as conn:
        got = await conn.fetchval("SELECT pg_try_advisory_lock($1)", 7741007)  # distinct lock key
        if got:
            try:
                await conn.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY recommendations_latest")
            finally:
                await conn.execute("SELECT pg_advisory_unlock($1)", 7741007)

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

async def refresher_loop_v2(player_pool: asyncpg.Pool, poll_seconds: int = 60) -> None:
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
                watermark = await conn.fetchval("SELECT max(computed_at) FROM fair_value_mv")
            if watermark and watermark != last_watermark:
                n = await run_pass_v2(player_pool)
                log.info("recommendation_engine_v2 pass: %d evaluations written", n)
                last_watermark = watermark
        except Exception as e:  # never let the loop die
            log.error("recommendation_engine_v2 refresher iteration failed: %s", e)
        await asyncio.sleep(poll_seconds)
