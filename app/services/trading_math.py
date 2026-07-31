"""
Central deterministic trading math for the Recommendation Engine V1.2.

This module is the ONLY place tax, ROI, break-even, EA price-increment
rounding, and the five composite scores (valuation/momentum/liquidity/
confidence/risk) are calculated. Nothing else in the backend - API
routes, workers, or the frontend - may reimplement these formulas.
recommendation_engine.py's RuleV1Strategy.generate() previously computed
"expected_roi_pct" as `discount_pct * (confidence / 100)` with no tax
applied at all (see app/services/recommendation_engine.py:81-98) - a
pure pre-tax discount presented as an ROI, which is why a 2%-discounted,
high-confidence card could show "1.96% expected ROI" and a BUY
recommendation simultaneously, despite a 2% pre-tax gap being a
guaranteed net LOSS after EA's 5% sale tax (break-even needs >5.26%).
This module exists to make that class of bug structurally impossible:
every profit-bearing figure here is computed from net_sale_proceeds()
(tax already applied), never from a raw price gap.

All currency figures (prices, profit, ROI) use Decimal, not float, to
avoid floating-point edge errors landing a value on the wrong side of a
threshold (e.g. a BUY gate at exactly 0.03 minimum ROI). The five
composite scores are dimensionless normalized indices (not currency), so
their internal math (tanh/log) uses plain floats - see each score
function's docstring.

Every function that depends on data which may be missing returns None
(never 0) when that data is absent - a genuinely zero value (e.g. zero
completed sales) and an absent value (e.g. sales data never collected)
are different facts and must never collapse into the same return value.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, getcontext
from typing import List, Optional, Sequence, Tuple

getcontext().prec = 28

Number = Decimal | int | float | str


def _d(value: Number) -> Decimal:
    """Convert any numeric input to Decimal without the float-repr
    artifacts `Decimal(0.1)` produces - route floats/ints through str()
    first."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


# =============================================================================
# Constants
# =============================================================================

EA_TAX = Decimal("0.05")
EA_MIN_PRICE = Decimal("200")
EA_MAX_PRICE = Decimal("5000000")

# FLAGGED - UNVERIFIED FOR FC26: this table is carried over from the prior
# title's known increment pattern (see repository instructions) because no
# verified FC26 increment table exists elsewhere in this codebase as of
# this writing. Every boundary must be re-confirmed against real listing
# behaviour before this is trusted for anything beyond directional
# estimates. Entries are (upper_bound_inclusive, increment) - a price is
# rounded to the nearest multiple of the increment belonging to the first
# bracket whose upper bound is >= the price; prices above the last
# threshold use the last bracket's increment.
PRICE_INCREMENT_TABLE: List[Tuple[Decimal, Decimal]] = [
    (Decimal("1000"), Decimal("50")),
    (Decimal("10000"), Decimal("100")),
    (Decimal("50000"), Decimal("250")),
    (Decimal("100000"), Decimal("500")),
    (Decimal("200000"), Decimal("1000")),
    (Decimal("1000000"), Decimal("5000")),
    (Decimal("5000001"), Decimal("10000")),
]

# Score-calibration constants. FLAGGED - none of these have been tuned
# against real outcome data yet (there is no outcome history to tune
# against until ml_labels starts accumulating closed windows - see Phase
# 3/7). They are reasonable, documented starting points, not verified
# thresholds - revisit once label data exists.
MOMENTUM_WINDOW_HOURS = 48
MOMENTUM_MIN_OBSERVATIONS = 4
MOMENTUM_MIN_SPAN_HOURS = 12
MOMENTUM_SLOPE_SCALE = 500.0

LIQUIDITY_REFERENCE_RATE = 2.0
LIQUIDITY_MIN_RATE_FLOOR = 0.01
LIQUIDITY_ACCEL_REFERENCE = 2.0

CONFIDENCE_SAMPLE_SATURATION_POINT = 20
# Must stay equal to strategy_config.py's MAX_ACCEPTABLE_STALENESS_MINUTES
# (raised from 60 to 90 there - see that constant's comment for why) -
# letting these drift apart is what turned one stale-price condition into
# two compounding failure reasons (STALE_PRICE from the hard gate, plus
# this freshness_component hitting exactly 0 at the old 60-min cutoff,
# zeroing the whole geometric-blend confidence score into LOW_CONFIDENCE)
# rather than the same root cause reported once.
CONFIDENCE_MAX_ACCEPTABLE_STALENESS_MIN = 90
CONFIDENCE_SAMPLE_WEIGHT = 0.40
CONFIDENCE_FRESHNESS_WEIGHT = 0.35
CONFIDENCE_CONSISTENCY_WEIGHT = 0.25

POPULARITY_GAMES_SATURATION_POINT = 5000  # games_played at/above this saturates the usage component
POPULARITY_GOALS_REFERENCE = 1.0  # avg_goals per game at/above this saturates the output component
POPULARITY_GAMES_WEIGHT = 0.6
POPULARITY_GOALS_WEIGHT = 0.4
assert abs(
    CONFIDENCE_SAMPLE_WEIGHT + CONFIDENCE_FRESHNESS_WEIGHT + CONFIDENCE_CONSISTENCY_WEIGHT - 1.0
) < 1e-9, "confidence score weights must sum to 1"

RISK_MIN_DOWNSIDE_OBSERVATIONS = 3
RISK_VOLATILITY_REFERENCE = 0.05
RISK_STALENESS_WEIGHT = 0.3
RISK_VOLATILITY_WEIGHT = 0.7


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


# =============================================================================
# EA price-increment rounding
# =============================================================================

def _increment_for(price: Decimal) -> Decimal:
    for threshold, increment in PRICE_INCREMENT_TABLE:
        if price <= threshold:
            return increment
    return PRICE_INCREMENT_TABLE[-1][1]


def round_to_ea_increment(price: Number, direction: str = "nearest") -> Decimal:
    """Round `price` to a valid EA listing increment, clamped to
    [EA_MIN_PRICE, EA_MAX_PRICE]. `direction`: "up" | "down" | "nearest".
    Clamping happens both before bracket lookup (so an out-of-range price
    doesn't pick the wrong bracket) and after rounding (so rounding up
    near the max, or down near the min, can't push the result out of
    EA's tradeable range)."""

    if direction not in ("up", "down", "nearest"):
        raise ValueError(f"invalid direction: {direction!r}")

    clamped = min(max(_d(price), EA_MIN_PRICE), EA_MAX_PRICE)
    increment = _increment_for(clamped)
    units = clamped / increment

    if direction == "up":
        rounded_units = units.to_integral_value(rounding=ROUND_CEILING)
    elif direction == "down":
        rounded_units = units.to_integral_value(rounding=ROUND_FLOOR)
    else:
        rounded_units = units.to_integral_value(rounding=ROUND_HALF_UP)

    result = rounded_units * increment
    return min(max(result, EA_MIN_PRICE), EA_MAX_PRICE)


# =============================================================================
# Tax / ROI / break-even
# =============================================================================

def net_sale_proceeds(sale_price: Number) -> Decimal:
    """What actually lands in the seller's coin balance after EA's 5% tax."""
    return _d(sale_price) * (Decimal("1") - EA_TAX)


def net_profit(sale_price: Number, entry_price: Number) -> Decimal:
    return net_sale_proceeds(sale_price) - _d(entry_price)


def net_roi(sale_price: Number, entry_price: Number) -> Optional[Decimal]:
    entry = _d(entry_price)
    if entry == 0:
        return None
    return net_profit(sale_price, entry_price) / entry


def break_even_sale_price(entry_price: Number) -> Decimal:
    """The minimum sale price that recovers `entry_price` after tax,
    rounded UP to the nearest valid EA increment (rounding down would
    produce a price that still nets a loss)."""
    raw = _d(entry_price) / (Decimal("1") - EA_TAX)
    return round_to_ea_increment(raw, direction="up")


def strategy_target_price(entry_price: Number, minimum_net_roi: Number) -> Decimal:
    """The minimum sale price required to clear both EA tax and a
    strategy's minimum required net ROI, rounded up (this is a required
    minimum, not an estimate - rounding down would understate what's
    actually required)."""
    entry = _d(entry_price)
    raw = entry * (Decimal("1") + _d(minimum_net_roi)) / (Decimal("1") - EA_TAX)
    return round_to_ea_increment(raw, direction="up")


# =============================================================================
# Completed-sale percentiles (matches Postgres percentile_cont's linear
# interpolation, since fair_value_mv's own fair_value_24h/7d are computed
# with percentile_cont(0.5) - keeping the same interpolation method here
# means these percentiles are directly comparable to the ones already in
# the database, not a subtly different statistic with the same name).
# =============================================================================

def percentile(sorted_values: Sequence[Number], pct: Number) -> Optional[Decimal]:
    """`sorted_values` must already be sorted ascending. `pct` in [0, 1]."""
    n = len(sorted_values)
    if n == 0:
        return None

    values = [_d(v) for v in sorted_values]
    if n == 1:
        return values[0]

    p = _d(pct)
    if p < 0 or p > 1:
        raise ValueError("pct must be within [0, 1]")

    rank = p * Decimal(n - 1)
    lower_idx = int(rank.to_integral_value(rounding=ROUND_FLOOR))
    upper_idx = min(lower_idx + 1, n - 1)
    frac = rank - Decimal(lower_idx)

    lower_val = values[lower_idx]
    upper_val = values[upper_idx]
    return lower_val + frac * (upper_val - lower_val)


MIN_SALES_FOR_PERCENTILES = 8


@dataclass(frozen=True)
class ScenarioPrices:
    conservative_price: Decimal
    likely_price: Decimal
    bullish_price: Decimal
    potential_price: Decimal
    sales_sample_size: int
    sales_window: str  # "24h" | "7d"


def scenario_prices(
    sales_24h: Sequence[Number],
    sales_7d: Sequence[Number],
    fair_value_24h: Optional[Number],
    fair_value_7d: Optional[Number],
) -> Optional[ScenarioPrices]:
    """Requires >= MIN_SALES_FOR_PERCENTILES in the trailing 24h; widens
    to trailing 7d if the 24h sample is too thin. Returns None
    (INSUFFICIENT_COMPLETED_SALES) if even the 7d sample is too thin."""

    sample = sorted(_d(v) for v in sales_24h)
    window = "24h"
    if len(sample) < MIN_SALES_FOR_PERCENTILES:
        sample = sorted(_d(v) for v in sales_7d)
        window = "7d"
    if len(sample) < MIN_SALES_FOR_PERCENTILES:
        return None

    if fair_value_24h is None or fair_value_7d is None:
        return None

    fair_value_blend = _d(fair_value_24h) * Decimal("0.6") + _d(fair_value_7d) * Decimal("0.4")

    bullish = percentile(sample, Decimal("0.80"))
    likely = percentile(sample, Decimal("0.50"))
    conservative = percentile(sample, Decimal("0.25"))
    assert bullish is not None and likely is not None and conservative is not None

    return ScenarioPrices(
        conservative_price=conservative,
        likely_price=likely,
        bullish_price=bullish,
        potential_price=fair_value_blend,
        sales_sample_size=len(sample),
        sales_window=window,
    )


def historical_fraction_at_or_above_likely(sales: Sequence[Number], likely_price: Number) -> Optional[float]:
    """V1.1's p_reach_likely, retained ONLY as a feature-logging/rule-
    calibration diagnostic. Because likely_price is itself the P50 of
    this same sample, this is close to 0.5 by construction - it is NOT a
    forward probability and must never be surfaced to users as one. Name
    kept deliberately unambiguous."""
    if not sales:
        return None
    threshold = _d(likely_price)
    at_or_above = sum(1 for s in sales if _d(s) >= threshold)
    return at_or_above / len(sales)


# =============================================================================
# Valuation score
# =============================================================================

def valuation_score(
    fair_value_24h: Optional[Number],
    entry_price: Optional[Number],
    likely_price: Optional[Number],
    bin_zscore_24h: Optional[Number],
) -> Optional[float]:
    """Dimensionless index in [-1, 1] - is entry_price cheap relative to
    evidence? Plain float math (tanh/clamp), not Decimal: this is a
    normalized score, not a currency amount, so float precision is not a
    financial-correctness concern here the way it is for ROI/tax."""

    if fair_value_24h is None or entry_price is None or likely_price is None or bin_zscore_24h is None:
        return None

    fv = float(fair_value_24h)
    entry = float(entry_price)
    likely = float(likely_price)
    zscore = float(bin_zscore_24h)

    if fv == 0 or likely == 0:
        return None

    fair_value_gap = (fv - entry) / fv
    clearing_gap = (likely - entry) / likely
    zscore_cheapness = _clamp((-zscore) / 3.0, -1.0, 1.0)

    score = (
        0.45 * math.tanh(fair_value_gap * 5)
        + 0.35 * math.tanh(clearing_gap * 5)
        + 0.20 * zscore_cheapness
    )
    return _clamp(score, -1.0, 1.0)


# =============================================================================
# Momentum score
# =============================================================================

def _linear_regression_slope(xs: List[float], ys: List[float]) -> float:
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator == 0:
        return 0.0
    return numerator / denominator


def momentum_score(
    bin_observations: Sequence[Tuple[datetime, Number]],
    current_lowest_bin: Optional[Number],
    trend_falling: bool,
    *,
    as_of: Optional[datetime] = None,
    window_hours: int = MOMENTUM_WINDOW_HOURS,
    min_observations: int = MOMENTUM_MIN_OBSERVATIONS,
    min_span_hours: float = MOMENTUM_MIN_SPAN_HOURS,
    slope_scale: float = MOMENTUM_SLOPE_SCALE,
) -> Optional[float]:
    """`bin_observations` is (timestamp, lowest_bin) pairs, any order.
    Returns None (INSUFFICIENT_BIN_HISTORY) if there aren't enough
    observations or they don't span enough time to fit a meaningful
    slope. Never derived from bin_zscore_24h - that measures distance
    from the sold-price distribution, not price direction over time."""

    if current_lowest_bin is None or not bin_observations:
        return None

    ordered = sorted(bin_observations, key=lambda pair: pair[0])
    reference = as_of or ordered[-1][0]
    cutoff = reference.timestamp() - window_hours * 3600
    windowed = [(ts, price) for ts, price in ordered if ts.timestamp() >= cutoff]

    if len(windowed) < min_observations:
        return None

    span_hours = (windowed[-1][0] - windowed[0][0]).total_seconds() / 3600.0
    if span_hours < min_span_hours:
        return None

    base_ts = windowed[0][0]
    elapsed_hours = [(ts - base_ts).total_seconds() / 3600.0 for ts, _ in windowed]
    prices = [float(price) for _, price in windowed]

    slope_per_hour = _linear_regression_slope(elapsed_hours, prices)

    lowest_bin = float(current_lowest_bin)
    if lowest_bin == 0:
        return None

    normalized_slope = slope_per_hour / lowest_bin
    momentum_raw = math.tanh(normalized_slope * slope_scale)

    if trend_falling:
        return min(momentum_raw, -0.1)
    return momentum_raw


# =============================================================================
# Liquidity score
# =============================================================================

def liquidity_score(
    sales_per_hour: Optional[Number],
    sales_count_24h: Optional[int],
    sales_count_7d: Optional[int],
) -> Optional[float]:
    """sales_count_24h is the authority on "do we genuinely know sales
    activity": if it's None, sales data was never collected for this
    card and liquidity is UNAVAILABLE, not 0. If it's a real 0, that's a
    real, meaningful illiquid-card signal and IS scored as 0."""

    if sales_count_24h is None:
        return None

    if sales_count_24h == 0:
        return 0.0

    spr = float(sales_per_hour) if sales_per_hour is not None else 0.0
    hourly_component = math.log(1 + spr) / math.log(1 + LIQUIDITY_REFERENCE_RATE)

    recent_rate_24h = sales_count_24h / 24.0
    trailing_rate_7d = (sales_count_7d or 0) / 168.0
    acceleration_ratio = recent_rate_24h / max(trailing_rate_7d, LIQUIDITY_MIN_RATE_FLOOR)

    acceleration_component = _clamp(
        math.log(acceleration_ratio) / math.log(LIQUIDITY_ACCEL_REFERENCE),
        -0.3,
        0.3,
    )

    return _clamp(hourly_component + acceleration_component, 0.0, 1.0)


# =============================================================================
# Confidence score
# =============================================================================

def confidence_score(
    sales_count_24h: Optional[int],
    sales_count_7d: Optional[int],
    price_age_minutes: Optional[Number],
    bin_zscore_24h: Optional[Number],
) -> Optional[float]:
    """Assumes data_quality_suspect has ALREADY been checked as a hard
    gate upstream (see RecommendationEngine's hard-gate step) - it is
    deliberately NOT re-included as a penalty here, to avoid double-
    counting the same corrupted-data signal once as a hard failure and
    again as a soft score deduction."""

    if (
        sales_count_24h is None
        or sales_count_7d is None
        or price_age_minutes is None
        or bin_zscore_24h is None
    ):
        return None

    sample_component = min(sales_count_24h, CONFIDENCE_SAMPLE_SATURATION_POINT) / CONFIDENCE_SAMPLE_SATURATION_POINT

    freshness_component = 1.0 - _clamp(
        float(price_age_minutes) / CONFIDENCE_MAX_ACCEPTABLE_STALENESS_MIN, 0.0, 1.0
    )

    sales_consistency = 1.0 - _clamp(
        abs(sales_count_24h * 7 - sales_count_7d) / max(sales_count_7d, 1), 0.0, 1.0
    )
    zscore_consistency = 1.0 - _clamp(abs(float(bin_zscore_24h)) / 5.0, 0.0, 1.0)
    consistency_component = 0.5 * sales_consistency + 0.5 * zscore_consistency

    # Geometric blend: any single component near 0 (e.g. wildly stale
    # price) pulls the whole score toward 0, rather than an arithmetic
    # mean letting one bad component hide behind two good ones.
    components = (
        (max(sample_component, 0.0), CONFIDENCE_SAMPLE_WEIGHT),
        (max(freshness_component, 0.0), CONFIDENCE_FRESHNESS_WEIGHT),
        (max(consistency_component, 0.0), CONFIDENCE_CONSISTENCY_WEIGHT),
    )
    if any(base == 0.0 for base, _ in components):
        return 0.0

    log_sum = sum(weight * math.log(base) for base, weight in components)
    return math.exp(log_sum)


# =============================================================================
# Risk score
# =============================================================================

def risk_score(
    bin_price_series: Sequence[Number],
    current_lowest_bin: Optional[Number],
    volatility_24h: Optional[Number],
    price_age_minutes: Optional[Number],
) -> Optional[float]:
    """`bin_price_series` is lowest_bin values in chronological order
    (already ordered by caller). Prefers realised downside volatility
    (the stddev of only the NEGATIVE consecutive deltas) over the
    symmetric volatility_24h fallback, since risk should weight the
    downside specifically, not overall dispersion. Does NOT include
    bin-zscore anomaly or a data-quality penalty - those are valuation/
    hard-gate concerns respectively, not downside-risk concerns."""

    if current_lowest_bin is None or price_age_minutes is None:
        return None

    lowest_bin = float(current_lowest_bin)
    if lowest_bin == 0:
        return None

    deltas = [
        float(bin_price_series[i]) - float(bin_price_series[i - 1])
        for i in range(1, len(bin_price_series))
    ]
    downside_deltas = [d for d in deltas if d < 0]

    downside_volatility: Optional[float] = None
    if len(downside_deltas) >= RISK_MIN_DOWNSIDE_OBSERVATIONS:
        mean = sum(downside_deltas) / len(downside_deltas)
        variance = sum((d - mean) ** 2 for d in downside_deltas) / len(downside_deltas)
        downside_volatility = math.sqrt(variance) / lowest_bin
    elif volatility_24h is not None:
        downside_volatility = float(volatility_24h) / lowest_bin

    if downside_volatility is None:
        return None

    volatility_component = _clamp(downside_volatility / RISK_VOLATILITY_REFERENCE, 0.0, 1.0)
    staleness_component = _clamp(
        float(price_age_minutes) / CONFIDENCE_MAX_ACCEPTABLE_STALENESS_MIN, 0.0, 1.0
    )

    return _clamp(
        RISK_VOLATILITY_WEIGHT * volatility_component + RISK_STALENESS_WEIGHT * staleness_component,
        0.0,
        1.0,
    )


# =============================================================================
# Popularity score
# =============================================================================

def popularity_score(
    games_played: Optional[int],
    avg_goals: Optional[Number],
) -> Optional[float]:
    """In-game usage/output signal, from fut_players.games_played_console/
    pc and avg_goals_console/pc (bin_sales_history_sync.py's own bio-text
    parse - no assists/win-rate data is scraped, so this is the only
    honest input available). None when games_played is missing - a card
    nobody has usage data for is UNRANKED, not "unpopular" (0). A real 0
    games_played is a genuine signal and is scored as 0."""

    if games_played is None:
        return None

    if games_played <= 0:
        return 0.0

    usage_component = _clamp(
        math.log(1 + games_played) / math.log(1 + POPULARITY_GAMES_SATURATION_POINT), 0.0, 1.0
    )
    output_component = _clamp(float(avg_goals or 0) / POPULARITY_GOALS_REFERENCE, 0.0, 1.0)

    return _clamp(
        POPULARITY_GAMES_WEIGHT * usage_component + POPULARITY_GOALS_WEIGHT * output_component,
        0.0,
        1.0,
    )
