# app/services/futgg_trend.py
"""
Trend / falling-knife assessment for the FUT.GG market layer.

WHY THIS EXISTS
---------------
The buy-side engine is a mispricing detector: it asks "is the lowest
current BIN below the median of recent sales". In a market that trends -
and FUT prices trend hard and persistently (post-promo decay, TOTW
cycles, SBC floods, end-of-cycle drift) - that question has a systematic
failure mode. A card that has fallen 25% over ten days still has a
stale-high median, so it reads as a large discount right up until it
falls another 25%. The engine had no trend term at all, so it would
reliably flag falling knives as its *best* opportunities: the further a
card had fallen, the bigger the apparent edge.

This module is the fix, and it is deliberately built as an independent,
separately-versioned layer rather than a bonus/penalty bolted onto the
existing score. Two reasons:

  1. A trend state is independently gradeable. "Was this card actually
     still falling 24h later" is answerable from market data alone,
     without reference to whether the buy engine agreed. Keeping the
     calculation separate and versioned (TREND_VERSION) means the outcome
     loop can validate the trend layer on its own terms.

  2. An opaque numeric adjustment is untunable. A named state with the
     features that produced it is inspectable by a user ("still falling")
     and by us.

DATA SEMANTICS
--------------
Input is raw (sold_price, sold_at) observations from futgg_sales_history,
NOT the pre-aggregated snapshot view - the aggregates cannot express a
slope. Two properties of that data drive the design:

  * `approximate_sold_at` is approximate by construction (derived from
    FUT.GG's "N minutes ago" text). It is good enough for ordering and
    for coarse time-weighting, which is all this module uses it for.

  * Observations are NOT evenly spaced. A card can have thirty sales in
    an hour then nothing for two days. Every feature here is therefore
    computed against real elapsed time - the window is split by time
    span, not by observation count, and the slope is a least-squares fit
    against actual hours rather than an index. Splitting by count on
    unevenly-spaced data silently compares "the last 20 sales" (which may
    span ten minutes) against "the previous 20" (which may span a week).

evaluate_trend() is pure: observations in, dataclass out, no I/O.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.services.futgg_config import TrendConfig, TREND_CONFIG

# Trend states, most-bearish to most-bullish, plus the honest "don't know".
FALLING_KNIFE = "falling_knife"
DOWNTREND = "downtrend"
STABILISING = "stabilising"
RECOVERING = "recovering"
SIDEWAYS = "sideways"
UPTREND = "uptrend"
INSUFFICIENT_TREND_DATA = "insufficient_trend_data"

TREND_STATES = (
    FALLING_KNIFE, DOWNTREND, STABILISING, RECOVERING,
    SIDEWAYS, UPTREND, INSUFFICIENT_TREND_DATA,
)

# Plain-English explanation per state. Rendered directly in the UI - the
# user should never see a raw state token.
STATE_DESCRIPTIONS: Dict[str, str] = {
    FALLING_KNIFE: (
        "This card is falling fast and has not stopped. The discount against "
        "recent sales is because the price is still dropping, not because it is "
        "underpriced."
    ),
    DOWNTREND: (
        "The price has been drifting down over this sales window. Recent sales "
        "are still below the earlier ones."
    ),
    STABILISING: (
        "The price fell but has now flattened out - recent sales have stopped "
        "making new lows."
    ),
    RECOVERING: (
        "The price dropped and has started climbing back off its low."
    ),
    SIDEWAYS: (
        "The price has been broadly flat across this sales window."
    ),
    UPTREND: (
        "The price has been climbing across this sales window."
    ),
    INSUFFICIENT_TREND_DATA: (
        "There are not enough recent sales, spread over enough time, to tell "
        "which way this card is moving."
    ),
}

# States in which a discount to the median is not trustworthy evidence of
# value. Consumed by futgg_intelligence's gating - kept here so the
# "which states are dangerous" judgement lives with the states themselves.
BEARISH_UNRESOLVED_STATES = frozenset({FALLING_KNIFE, DOWNTREND})


@dataclass(frozen=True)
class TrendFeatures:
    """Every raw input behind the state, kept on the result so a
    recommendation snapshot can persist them and the outcome loop can ask
    which feature actually carried predictive weight."""

    observation_count: int = 0
    span_minutes: Optional[float] = None

    # Median of the recent time-slice vs the earlier time-slice. The
    # primary "which way is this going" measure.
    medium_term_change: Optional[float] = None
    # Change *within* the recent slice - is the move still going, or has
    # it flattened? This is what separates a knife from a stabilised dip.
    short_term_change: Optional[float] = None

    # Latest observed sale against the whole window's median.
    latest_vs_median: Optional[float] = None

    # Least-squares slope of price against real elapsed hours, expressed
    # as a fraction of mean price per hour (so it is comparable across
    # bronze and special tiers).
    slope_pct_per_hour: Optional[float] = None

    # Share of recent-slice sales that printed below the earlier slice's
    # median - a distribution-level confirmation that the level has
    # genuinely moved rather than one outlier dragging a mean.
    below_prior_median_ratio: Optional[float] = None

    # Peak-to-latest decline within the window.
    drawdown_from_high: Optional[float] = None
    # Latest against the window low - reversal evidence.
    bounce_from_low: Optional[float] = None
    # Share of recent-slice observations that set a new running low.
    # Near zero means the market has stopped finding lower clearing
    # prices, which is the operational definition of stabilising here.
    new_low_ratio: Optional[float] = None

    # Sales per hour in the recent slice divided by the earlier slice.
    # >1 means trading is accelerating.
    velocity_ratio: Optional[float] = None

    # Coefficient of variation per slice - a tightening spread supports
    # the stabilising read.
    dispersion_earlier: Optional[float] = None
    dispersion_recent: Optional[float] = None

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TrendAssessment:
    state: str
    version: str
    features: TrendFeatures
    description: str
    # True when the state is one where a discount to the median should not
    # be read as value. Convenience for callers so the set membership rule
    # lives in one place.
    blocks_value_read: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "version": self.version,
            "description": self.description,
            "blocks_value_read": self.blocks_value_read,
            "features": self.features.as_dict(),
        }


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _median(values: Sequence[float]) -> Optional[float]:
    vals = [float(v) for v in values]
    return statistics.median(vals) if vals else None


def _dispersion(values: Sequence[float]) -> Optional[float]:
    """Coefficient of variation. None for samples too small to have a
    meaningful spread, rather than a fabricated 0.0."""
    vals = [float(v) for v in values]
    if len(vals) < 2:
        return None
    mean = statistics.fmean(vals)
    if mean <= 0:
        return None
    return statistics.pstdev(vals) / mean


def _slope_pct_per_hour(points: Sequence[Tuple[float, float]]) -> Optional[float]:
    """Least-squares slope of price against elapsed hours, normalised by
    mean price. `points` is (hours_since_start, price).

    Normalising by mean price is what makes this comparable across price
    levels - a 2,000-coin/hour drift is catastrophic on a 20k card and
    noise on a 2M one.
    """
    if len(points) < 2:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    mean_x = statistics.fmean(xs)
    mean_y = statistics.fmean(ys)
    if mean_y <= 0:
        return None
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator <= 0:
        # Every observation shares a timestamp - no time axis to fit.
        return None
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    return (numerator / denominator) / mean_y


def _relative_change(later: Optional[float], earlier: Optional[float]) -> Optional[float]:
    if later is None or earlier is None or earlier <= 0:
        return None
    return (later - earlier) / earlier


def _split_by_time(
    observations: List[Tuple[float, datetime]], recent_fraction: float,
) -> Tuple[List[Tuple[float, datetime]], List[Tuple[float, datetime]]]:
    """Split chronologically-sorted observations into (earlier, recent) at
    a point defined by elapsed TIME, not observation count.

    This is the whole reason the module refuses to index-split: with
    unevenly spaced sales, "the last 40% of rows" and "the last 40% of the
    window" can be wildly different periods, and only the latter answers
    "what has this card been doing lately".
    """
    start = observations[0][1]
    end = observations[-1][1]
    total_seconds = (end - start).total_seconds()
    if total_seconds <= 0:
        return observations, observations
    cutoff_seconds = total_seconds * (1.0 - recent_fraction)
    cutoff = start.timestamp() + cutoff_seconds
    earlier = [o for o in observations if o[1].timestamp() <= cutoff]
    recent = [o for o in observations if o[1].timestamp() > cutoff]
    # Guarantee both slices are non-empty so downstream medians are real.
    # A pathological distribution (everything on one side of the cutoff)
    # falls back to a half-and-half index split, which is still better
    # than returning an empty slice and None-ing every feature.
    if not earlier or not recent:
        midpoint = max(1, len(observations) // 2)
        earlier, recent = observations[:midpoint], observations[midpoint:]
        if not recent:
            recent = earlier[-1:]
    return earlier, recent


def _insufficient(count: int, span_minutes: Optional[float], config: TrendConfig) -> TrendAssessment:
    return TrendAssessment(
        state=INSUFFICIENT_TREND_DATA,
        version=config.version,
        features=TrendFeatures(observation_count=count, span_minutes=span_minutes),
        description=STATE_DESCRIPTIONS[INSUFFICIENT_TREND_DATA],
        blocks_value_read=False,
    )


def evaluate_trend(
    sales: Sequence[Any],
    *,
    as_of: Optional[datetime] = None,
    config: Optional[TrendConfig] = None,
) -> TrendAssessment:
    """Assess the price trend from raw sales observations.

    `sales` accepts either mappings with "sold_price"/"approximate_sold_at"
    keys (the shape market_data_provider returns) or (price, datetime)
    tuples, so tests can build cases without dict boilerplate.

    Returns INSUFFICIENT_TREND_DATA rather than guessing whenever the
    sample is too thin or too time-compressed to support a real
    conclusion - consistent with the engine's existing principle of
    admitting ignorance instead of inventing certainty.
    """
    cfg = config or TREND_CONFIG
    as_of = as_of or datetime.now(timezone.utc)

    parsed: List[Tuple[float, datetime]] = []
    for row in sales or []:
        if isinstance(row, dict):
            price = row.get("sold_price")
            sold_at = row.get("approximate_sold_at") or row.get("sold_at")
        else:
            price, sold_at = row[0], row[1]
        if price is None or sold_at is None:
            continue
        try:
            price_f = float(price)
        except (TypeError, ValueError):
            continue
        if price_f <= 0:
            continue
        parsed.append((price_f, _as_utc(sold_at)))

    if len(parsed) < cfg.min_observations:
        return _insufficient(len(parsed), None, cfg)

    parsed.sort(key=lambda p: p[1])
    span_minutes = (parsed[-1][1] - parsed[0][1]).total_seconds() / 60.0
    if span_minutes < cfg.min_span_minutes:
        return _insufficient(len(parsed), span_minutes, cfg)

    prices = [p[0] for p in parsed]
    start_ts = parsed[0][1]
    hours = [(p[1] - start_ts).total_seconds() / 3600.0 for p in parsed]

    earlier, recent = _split_by_time(parsed, cfg.recent_fraction)
    earlier_prices = [p[0] for p in earlier]
    recent_prices = [p[0] for p in recent]

    prior_median = _median(earlier_prices)
    recent_median = _median(recent_prices)
    window_median = _median(prices)

    medium_term_change = _relative_change(recent_median, prior_median)

    # Short-term: split the recent slice again by time and compare. This
    # is the "has the move stopped" measure - a knife is still cutting
    # inside its own most recent period, a stabilised dip is not.
    short_term_change: Optional[float] = None
    if len(recent) >= 4:
        recent_earlier, recent_latest = _split_by_time(recent, 0.5)
        short_term_change = _relative_change(
            _median([p[0] for p in recent_latest]),
            _median([p[0] for p in recent_earlier]),
        )
    elif medium_term_change is not None:
        # Too few recent observations to sub-split honestly. Fall back to
        # the slope across the recent slice rather than fabricating a
        # sub-window comparison from two points.
        recent_hours = [(p[1] - recent[0][1]).total_seconds() / 3600.0 for p in recent]
        recent_slope = _slope_pct_per_hour(list(zip(recent_hours, recent_prices)))
        recent_span_hours = recent_hours[-1] if recent_hours else 0.0
        if recent_slope is not None and recent_span_hours > 0:
            short_term_change = recent_slope * recent_span_hours

    latest_price = prices[-1]
    window_high = max(prices)
    window_low = min(prices)

    below_prior_median_ratio = None
    if prior_median is not None and recent_prices:
        below_prior_median_ratio = sum(
            1 for p in recent_prices if p < prior_median
        ) / len(recent_prices)

    # New lows are computed against the running minimum of everything seen
    # BEFORE the recent slice began, so "new" genuinely means new.
    new_low_ratio = None
    if earlier_prices and recent_prices:
        running_low = min(earlier_prices)
        new_lows = 0
        for price in recent_prices:
            if price < running_low:
                new_lows += 1
                running_low = price
        new_low_ratio = new_lows / len(recent_prices)

    velocity_ratio = None
    if earlier and recent:
        earlier_span_h = max(
            (earlier[-1][1] - earlier[0][1]).total_seconds() / 3600.0, 1e-6
        )
        recent_span_h = max(
            (recent[-1][1] - recent[0][1]).total_seconds() / 3600.0, 1e-6
        )
        earlier_rate = len(earlier) / earlier_span_h
        recent_rate = len(recent) / recent_span_h
        if earlier_rate > 0:
            velocity_ratio = recent_rate / earlier_rate

    features = TrendFeatures(
        observation_count=len(parsed),
        span_minutes=span_minutes,
        medium_term_change=medium_term_change,
        short_term_change=short_term_change,
        latest_vs_median=_relative_change(latest_price, window_median),
        slope_pct_per_hour=_slope_pct_per_hour(list(zip(hours, prices))),
        below_prior_median_ratio=below_prior_median_ratio,
        drawdown_from_high=(
            (window_high - latest_price) / window_high if window_high > 0 else None
        ),
        bounce_from_low=(
            (latest_price - window_low) / window_low if window_low > 0 else None
        ),
        new_low_ratio=new_low_ratio,
        velocity_ratio=velocity_ratio,
        dispersion_earlier=_dispersion(earlier_prices),
        dispersion_recent=_dispersion(recent_prices),
    )

    state = _classify(features, cfg)
    return TrendAssessment(
        state=state,
        version=cfg.version,
        features=features,
        description=STATE_DESCRIPTIONS[state],
        blocks_value_read=state in BEARISH_UNRESOLVED_STATES,
    )


def _classify(f: TrendFeatures, cfg: TrendConfig) -> str:
    """Ordered state resolution. Order matters: the 'has it stopped?'
    states (stabilising/recovering) are tested before the plain
    directional ones, so a card that fell and then flattened is reported
    as stabilising rather than being lumped in with cards still falling.
    """
    medium = f.medium_term_change
    short = f.short_term_change

    if medium is None:
        return SIDEWAYS

    fell_over_window = medium <= cfg.downtrend_change_threshold

    # --- Falling knife: steep, broad-based, deep, and NOT flattening ----
    # All four conditions are required. Any one of them alone is a normal
    # feature of a noisy market; together they describe a price that is
    # actively still finding lower clearing levels.
    if (
        short is not None
        and short <= cfg.falling_knife_short_change
        and (f.below_prior_median_ratio or 0.0) >= cfg.falling_knife_below_prior_median_ratio
        and (f.drawdown_from_high or 0.0) >= cfg.falling_knife_min_drawdown
    ):
        return FALLING_KNIFE

    # --- Recovering: fell off a peak, turned back up off the low --------
    # Tested before the directional states because a V-shaped recovery's
    # start-to-end change is near zero (it would otherwise read as
    # sideways) and a late-stage one can be positive enough to read as an
    # uptrend. The distinguishing feature is that it is still trading
    # below its earlier high, which a genuine uptrend is not.
    if (
        short is not None
        and short > 0
        and (f.drawdown_from_high or 0.0) >= cfg.recovering_min_drawdown
        and (f.bounce_from_low or 0.0) >= cfg.recovering_min_bounce
    ):
        return RECOVERING

    # --- Stabilising: fell, then stopped making new lows ----------------
    if (
        fell_over_window
        and short is not None
        and abs(short) <= cfg.stabilising_max_recent_change
        and (f.new_low_ratio if f.new_low_ratio is not None else 1.0) <= cfg.stabilising_max_new_low_ratio
    ):
        return STABILISING

    if fell_over_window:
        return DOWNTREND
    if medium >= cfg.uptrend_change_threshold:
        return UPTREND
    if abs(medium) <= cfg.sideways_band:
        return SIDEWAYS
    # Between the sideways band and the directional thresholds - a real
    # but unconvincing drift. Reported by direction rather than being
    # forced into 'sideways', which would overstate how flat it is.
    return DOWNTREND if medium < 0 else UPTREND
