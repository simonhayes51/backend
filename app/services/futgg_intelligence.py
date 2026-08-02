# app/services/futgg_intelligence.py
"""
Fair value / recommendation logic for the FUT.GG-backed market layer.

Adapted from the same *shape* of output as recommendation_engine_v2.py
(signal / reasons / confidence / risk), but the inputs are FUT.GG's data
semantics, which differ from the legacy FUTBIN-backed fair_value_mv path
in two structural ways this module has to account for:

  1. There is no separate "24h vs 7d" sales split - futgg_sales_history
     only gives a bounded recent window (<=50 rows / 14 days, see
     migrations/038). So confidence/sample-size scoring here works off a
     single window's count + the actual time span it covers (a card with
     50 sales in the last 20 minutes is a very different liquidity signal
     than 50 sales spread over 14 days, even though both are "n=50").

  2. approximate_sold_at is, structurally, never exact (see
     futgg_sales_history's own column comment) - this module already
     only ever consumes the pre-aggregated snapshot view's derived
     stats, never raw per-row timestamps, so this mostly matters for the
     router layer (which must label sales rows as approximate) rather
     than here - but it's still why "latest sale age" is treated as an
     approximate freshness signal, not an exact one, in the confidence
     calc below.

All tax/ROI/EA-increment math is delegated to app.services.trading_math
- nothing here hand-rolls `* 0.95` or a break-even formula.

evaluate_card() is pure (dict in, dataclass out) and fully unit-testable
with constructed snapshot dicts - no I/O. The I/O (reading
futgg_market_snapshot) lives in market_data_provider.py; the router
layer wires the two together.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from app.services import trading_math as tm

# =============================================================================
# Thresholds - documented starting points (no closed-outcome history to
# tune against yet for FUT.GG specifically, same caveat trading_math.py
# and strategy_config.py both carry for the legacy engine's constants).
# =============================================================================

# Below this many sales in the bounded recent window, the sample is too
# thin to trust a median/trimmed-mean over - matches recommendation_
# engine_v2's MIN_SALES_24H_FLOOR=3 order of magnitude, nudged up
# slightly because FUT.GG's window can span up to 14 days (a thin sample
# there is weaker evidence than the same count in a tight 24h window).
MIN_SALES_FOR_SIGNAL = 5

# A price observation older than this is not acted on - mirrors trading_
# math's CONFIDENCE_MAX_ACCEPTABLE_STALENESS_MIN (90) but slightly wider
# since FUT.GG's own next_price_due_at cadence is tier-dependent and can
# legitimately be longer than the legacy FUTBIN poller's.
MAX_ACCEPTABLE_PRICE_AGE_MINUTES = 120

# sales_dispersion_ratio (stddev / median) at or above this is treated as
# extreme - the recent-sales sample is too volatile for its median to be
# a trustworthy fair-value anchor on its own.
EXTREME_DISPERSION_RATIO = 0.45

MIN_CONFIDENCE_FOR_BUY_SIGNAL = 0.45
MIN_CONFIDENCE_FOR_ANY_SIGNAL = 0.20

# Minimum net ROI (after EA tax, via trading_math) the *conservative* side
# of fair value needs to clear before a buy-side signal is issued -
# mirrors strategy_config.py's quick_flip floor.
MIN_NET_ROI_FOR_BUY = Decimal("0.03")
MIN_NET_ROI_FOR_STRONG_BUY = Decimal("0.08")

# Weights for the fair-value blend between the recent-sales estimate and
# the live BIN cross-check. Sales evidence dominates by design (BIN is a
# single current ask, not a cleared price), but a live BIN with none/thin
# sales is still useful corroboration once confidence-weighted below.
FAIR_VALUE_SALES_WEIGHT = 0.7
FAIR_VALUE_BIN_WEIGHT = 0.3

RISK_LEVELS = ("low", "medium", "high", "avoid")
SIGNALS = ("strong_buy", "buy", "watch", "hold", "sell", "avoid", "insufficient_data")


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass
class CardIntelligence:
    card_id: int
    fair_value: Optional[Decimal]
    recommended_buy_max: Optional[Decimal]
    recommended_sell_target: Optional[Decimal]
    expected_profit_after_tax: Optional[Decimal]
    expected_roi: Optional[Decimal]
    liquidity_score: Optional[float]
    confidence_score: Optional[float]
    risk_level: str
    signal: str
    signal_reasons: List[str] = field(default_factory=list)
    price_age_minutes: Optional[int] = None
    sales_sample_size: int = 0
    sales_window_span_minutes: Optional[float] = None


def _price_age_minutes(bin_captured_at: Optional[datetime], as_of: datetime) -> Optional[int]:
    if bin_captured_at is None:
        return None
    if bin_captured_at.tzinfo is None:
        bin_captured_at = bin_captured_at.replace(tzinfo=timezone.utc)
    delta = as_of - bin_captured_at
    return max(0, int(delta.total_seconds() // 60))


def _compute_liquidity_score(sales_count: int, span_minutes: Optional[float]) -> Optional[float]:
    """None only when the window itself is meaningless (no span despite
    sales existing shouldn't happen, but guards div-by-zero). A real 0
    sales count IS scored as 0.0 - a genuinely illiquid card, not
    "unknown"."""
    if sales_count == 0:
        return 0.0
    if not span_minutes or span_minutes <= 0:
        # A single sale (or all sales at the same approximate timestamp)
        # - can't derive a rate, but the raw count still carries some
        # signal via trading_math's own reference-rate curve at a
        # nominal 1-hour span.
        span_minutes = 60.0
    sales_per_hour = sales_count / (span_minutes / 60.0)
    return tm.liquidity_score(sales_per_hour, sales_count, sales_count * 7)


def _compute_confidence(
    *,
    sales_count: int,
    price_age_minutes: Optional[int],
    dispersion_ratio: Optional[float],
    is_tradeable: Optional[bool],
) -> float:
    """Geometric-blend confidence in [0, 1], same "one bad component
    tanks the whole score" philosophy as trading_math.confidence_score,
    reimplemented here (not called directly) because FUT.GG's inputs
    don't map onto that function's exact parameter set (no 24h/7d split,
    dispersion ratio instead of a BIN z-score)."""
    if is_tradeable is False:
        return 0.0

    sample_component = min(sales_count, 20) / 20.0

    if price_age_minutes is None:
        freshness_component = 0.0
    else:
        freshness_component = 1.0 - _clamp(price_age_minutes / MAX_ACCEPTABLE_PRICE_AGE_MINUTES, 0.0, 1.0)

    if dispersion_ratio is None:
        dispersion_component = 0.5  # unknown dispersion - neutral, not penalized or rewarded
    else:
        dispersion_component = 1.0 - _clamp(dispersion_ratio / EXTREME_DISPERSION_RATIO, 0.0, 1.0)

    components = (
        (max(sample_component, 0.0), 0.40),
        (max(freshness_component, 0.0), 0.35),
        (max(dispersion_component, 0.0), 0.25),
    )
    if any(base == 0.0 for base, _ in components):
        return 0.0
    log_sum = sum(weight * math.log(base) for base, weight in components)
    return _clamp(math.exp(log_sum), 0.0, 1.0)


def _compute_risk_level(
    *,
    confidence: float,
    dispersion_ratio: Optional[float],
    price_age_minutes: Optional[int],
    sales_count: int,
    is_tradeable: Optional[bool],
) -> str:
    if is_tradeable is False:
        return "avoid"
    if sales_count < MIN_SALES_FOR_SIGNAL or price_age_minutes is None or price_age_minutes > MAX_ACCEPTABLE_PRICE_AGE_MINUTES:
        return "high"
    if dispersion_ratio is not None and dispersion_ratio >= EXTREME_DISPERSION_RATIO:
        return "high"
    if confidence >= 0.65 and (dispersion_ratio is None or dispersion_ratio < 0.20):
        return "low"
    return "medium"


def evaluate_card(snapshot: Dict[str, Any], *, as_of: Optional[datetime] = None) -> CardIntelligence:
    """Pure function: snapshot dict (one row of futgg_market_snapshot,
    or an equivalent constructed dict in tests) -> CardIntelligence.
    Never raises on missing/None fields - every optional input degrades
    to a documented "insufficient data" outcome instead."""
    as_of = as_of or datetime.now(timezone.utc)
    card_id = snapshot["source_card_id"]
    is_tradeable = snapshot.get("is_tradeable")

    reasons: List[str] = []

    if is_tradeable is False:
        return CardIntelligence(
            card_id=card_id, fair_value=None, recommended_buy_max=None,
            recommended_sell_target=None, expected_profit_after_tax=None,
            expected_roi=None, liquidity_score=None, confidence_score=0.0,
            risk_level="avoid", signal="avoid",
            signal_reasons=["Card is untradeable (SBC/objective reward) - never a live market target."],
            price_age_minutes=None, sales_sample_size=0, sales_window_span_minutes=None,
        )

    current_bin = snapshot.get("current_bin")
    bin_captured_at = snapshot.get("bin_captured_at")
    sales_count = int(snapshot.get("sales_count") or 0)
    sales_median = snapshot.get("sales_median")
    sales_trimmed_mean = snapshot.get("sales_trimmed_mean")
    span_minutes = snapshot.get("sales_window_span_minutes")
    dispersion_ratio = snapshot.get("sales_dispersion_ratio")
    dispersion_ratio = float(dispersion_ratio) if dispersion_ratio is not None else None

    price_age_minutes = _price_age_minutes(bin_captured_at, as_of)

    confidence = _compute_confidence(
        sales_count=sales_count, price_age_minutes=price_age_minutes,
        dispersion_ratio=dispersion_ratio, is_tradeable=is_tradeable,
    )
    risk_level = _compute_risk_level(
        confidence=confidence, dispersion_ratio=dispersion_ratio,
        price_age_minutes=price_age_minutes, sales_count=sales_count,
        is_tradeable=is_tradeable,
    )
    liquidity = _compute_liquidity_score(sales_count, span_minutes)

    # ---- Hard "can't recommend a trade" gates -----------------------------
    if sales_count < MIN_SALES_FOR_SIGNAL:
        reasons.append(f"Only {sales_count} recent sale(s) recorded - need at least {MIN_SALES_FOR_SIGNAL} to trust a price.")
    if price_age_minutes is None:
        reasons.append("No BIN price observation recorded for this card.")
    elif price_age_minutes > MAX_ACCEPTABLE_PRICE_AGE_MINUTES:
        reasons.append(f"Current price observation is {price_age_minutes} minutes old - beyond the {MAX_ACCEPTABLE_PRICE_AGE_MINUTES}-minute freshness limit.")
    if dispersion_ratio is not None and dispersion_ratio >= EXTREME_DISPERSION_RATIO:
        reasons.append(f"Recent sales dispersion ratio {dispersion_ratio:.2f} is extreme (>= {EXTREME_DISPERSION_RATIO}) - the price is too volatile to anchor a fair value on right now.")
    if current_bin is None:
        reasons.append("No live BIN listing found.")
    sales_estimate = sales_trimmed_mean if sales_trimmed_mean is not None else sales_median
    if sales_estimate is None:
        # Should be unreachable when sales_count >= MIN_SALES_FOR_SIGNAL,
        # since sales_count/sales_median/sales_trimmed_mean all come from
        # the same grouped query in futgg_market_snapshot - but this was
        # previously an `assert`, which is a real 500 (or, worse, a
        # silent no-op under `python -O`) if that invariant is ever
        # violated by a snapshot row this function didn't anticipate.
        # Degrade to the same "insufficient_data" outcome as every other
        # gate here instead of trusting an assumption about upstream SQL.
        reasons.append("No recent-sales price estimate available for this card.")

    insufficient = bool(reasons)

    if insufficient:
        return CardIntelligence(
            card_id=card_id, fair_value=None, recommended_buy_max=None,
            recommended_sell_target=None, expected_profit_after_tax=None,
            expected_roi=None, liquidity_score=liquidity, confidence_score=confidence,
            risk_level=risk_level, signal="insufficient_data", signal_reasons=reasons,
            price_age_minutes=price_age_minutes, sales_sample_size=sales_count,
            sales_window_span_minutes=span_minutes,
        )

    # ---- Fair value: confidence-weighted blend of sales evidence + BIN ----

    sales_weight = Decimal(str(FAIR_VALUE_SALES_WEIGHT))
    bin_weight = Decimal(str(FAIR_VALUE_BIN_WEIGHT))
    fair_value = Decimal(str(sales_estimate)) * sales_weight + Decimal(str(current_bin)) * bin_weight

    recommended_sell_target = tm.round_to_ea_increment(fair_value, direction="down")

    # Buy ceiling: the highest entry price that still clears
    # MIN_NET_ROI_FOR_BUY once sold at fair_value, after EA's tax. This is
    # exactly trading_math's net_roi() relation solved for entry_price
    # (net_sale_proceeds(fair_value) / entry - 1 >= MIN_NET_ROI_FOR_BUY),
    # still built entirely from tm.EA_TAX rather than a hand-rolled
    # constant, and rounded down (a ceiling should never round up past
    # what actually clears the bar).
    recommended_buy_max = tm.round_to_ea_increment(
        fair_value * (Decimal("1") - tm.EA_TAX) / (Decimal("1") + MIN_NET_ROI_FOR_BUY),
        direction="down",
    )

    expected_profit_after_tax = tm.net_profit(recommended_sell_target, current_bin)
    expected_roi = tm.net_roi(recommended_sell_target, current_bin)

    reasons = []
    bin_vs_median_pct = None
    if sales_median and sales_median > 0:
        bin_vs_median_pct = (float(sales_median) - float(current_bin)) / float(sales_median) * 100
        direction = "below" if bin_vs_median_pct > 0 else "above"
        reasons.append(f"Current BIN is {abs(bin_vs_median_pct):.1f}% {direction} the median of {sales_count} recent sales.")
    if span_minutes is not None and sales_count > 1:
        if span_minutes < 60:
            reasons.append(f"{sales_count} sales occurred within {span_minutes:.0f} minutes.")
        else:
            reasons.append(f"{sales_count} sales occurred over the last {span_minutes / 60.0:.1f} hours.")
    if price_age_minutes is not None:
        reasons.append(f"Current price observation is {price_age_minutes} minute(s) old.")
    if dispersion_ratio is not None:
        reasons.append(f"Recent sales dispersion ratio is {dispersion_ratio:.2f}.")

    # ---- Signal decision ---------------------------------------------------
    if expected_roi is None or expected_roi <= 0:
        signal = "avoid"
        reasons.append("Expected sell target does not clear EA's 5% tax at the current BIN - not a real edge.")
    elif confidence < MIN_CONFIDENCE_FOR_ANY_SIGNAL:
        signal = "insufficient_data"
        reasons.append(f"Confidence score {confidence:.2f} is below the minimum {MIN_CONFIDENCE_FOR_ANY_SIGNAL} needed to act on any signal.")
    elif confidence >= MIN_CONFIDENCE_FOR_BUY_SIGNAL and expected_roi >= MIN_NET_ROI_FOR_STRONG_BUY and risk_level in ("low", "medium"):
        signal = "strong_buy"
        reasons.append(f"Expected net ROI {float(expected_roi) * 100:.1f}% clears the strong-buy threshold ({float(MIN_NET_ROI_FOR_STRONG_BUY) * 100:.0f}%) with confidence {confidence:.2f}.")
    elif confidence >= MIN_CONFIDENCE_FOR_BUY_SIGNAL and expected_roi >= MIN_NET_ROI_FOR_BUY and risk_level != "high":
        signal = "buy"
        reasons.append(f"Expected net ROI {float(expected_roi) * 100:.1f}% clears the buy threshold ({float(MIN_NET_ROI_FOR_BUY) * 100:.0f}%) with confidence {confidence:.2f}.")
    elif expected_roi > 0 and confidence >= MIN_CONFIDENCE_FOR_ANY_SIGNAL:
        signal = "watch"
        reasons.append("Positive expected edge but below the buy-signal confidence/ROI/risk bar.")
    else:
        signal = "hold"
        reasons.append("No actionable edge in either direction right now.")

    return CardIntelligence(
        card_id=card_id, fair_value=fair_value.quantize(Decimal("1")),
        recommended_buy_max=recommended_buy_max, recommended_sell_target=recommended_sell_target,
        expected_profit_after_tax=expected_profit_after_tax, expected_roi=expected_roi,
        liquidity_score=liquidity, confidence_score=confidence, risk_level=risk_level,
        signal=signal, signal_reasons=reasons, price_age_minutes=price_age_minutes,
        sales_sample_size=sales_count, sales_window_span_minutes=span_minutes,
    )
