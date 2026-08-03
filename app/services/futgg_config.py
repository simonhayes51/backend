# app/services/futgg_config.py
"""
Single source of truth for every tunable the FUT.GG intelligence engine
uses, plus the version stamps that make an outcome-graded comparison
between two configurations possible.

Why this module exists (see futgg_intelligence.py's own header for the
longer version): thresholds and blend weights used to be module-level
constants scattered across futgg_intelligence.py. That made two things
impossible:

  1. Recording *which* configuration produced a given recommendation, so
     that when the outcome grader closes the loop weeks later we can
     attribute the result to something specific rather than "whatever
     the code happened to say at the time".

  2. Running two configurations against each other. Every threshold in
     here is a documented guess (there was no closed-outcome history to
     tune against when the engine was written) - the only way to stop
     guessing is to version the guesses and grade them.

ENGINE_VERSION must be bumped whenever a change would alter the
recommendation produced for an unchanged market snapshot. TREND_VERSION
is versioned separately because the trend layer is deliberately
independently testable/validatable (a trend state can be graded against
what the price actually did next, without reference to whether the
buy-side engine agreed).

Nothing in here does I/O or imports any other app module beyond
trading_math, so it stays safe to import from pure/unit-tested code.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from decimal import Decimal
from typing import Any, Dict, Mapping, Optional

# =============================================================================
# Version stamps
# =============================================================================
#
# Bump ENGINE_VERSION on any change that alters the recommendation for an
# unchanged snapshot. Format is "futgg-<major>.<minor>"; the outcome
# grader groups by this exact string, so it must be stable and unique per
# behavioural configuration.
ENGINE_VERSION = "futgg-2.0"

# Bumped independently of ENGINE_VERSION - see module docstring.
TREND_VERSION = "trend-1.0"

# Bumped when the outcome grading rules themselves change (e.g. a
# different definition of "entry achieved"). Stored on every outcome row
# so a later grading-rule change never silently makes old and new labels
# look comparable.
GRADER_VERSION = "grader-1.0"


def _env_decimal(name: str, default: str) -> Decimal:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return Decimal(default)
    try:
        return Decimal(raw.strip())
    except Exception:
        return Decimal(default)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


@dataclass(frozen=True)
class EngineConfig:
    """Every threshold/weight the buy-side engine reads. Frozen so a
    config can be safely shared across requests and hashed into a
    recommendation snapshot without risk of mutation mid-evaluation."""

    version: str = ENGINE_VERSION

    # ---- Sample-sufficiency gates -------------------------------------
    min_sales_for_signal: int = 5

    # ---- Staleness ----------------------------------------------------
    # Per-tier acceptable price age. Roughly a third of each tier's own
    # re-price interval in futgg_price_sync.py, so "acceptable" is always
    # meaningfully tighter than the worst realistic gap between scrapes.
    max_price_age_minutes_by_tier: Mapping[str, int] = field(
        default_factory=lambda: {
            "special": 20,
            "gold_rare": 45,
            "gold_common": 180,
            "silver": 480,
            "bronze": 720,
        }
    )
    default_max_price_age_minutes: int = 120

    # ---- Dispersion ---------------------------------------------------
    extreme_dispersion_ratio: float = 0.45

    # ---- Confidence gates ---------------------------------------------
    min_confidence_for_buy_signal: float = 0.45
    min_confidence_for_any_signal: float = 0.20

    # ---- ROI gates ----------------------------------------------------
    min_net_roi_for_buy: Decimal = Decimal("0.03")
    min_net_roi_for_strong_buy: Decimal = Decimal("0.08")

    # ---- Fair-value blend ---------------------------------------------
    # Sales evidence dominates by design: BIN is a single current ask,
    # not a cleared price. The BIN weight is a deliberate haircut on the
    # apparent edge - it pulls fair value toward the live ask, so a card
    # listed far below its sales median does not get credited with the
    # full gap. Conservative, and explicitly one of the first things the
    # outcome loop should be used to validate or correct.
    fair_value_sales_weight: float = 0.7
    fair_value_bin_weight: float = 0.3

    # ---- Conservative buy margin --------------------------------------
    # The recommended (as opposed to theoretical-maximum) entry sits this
    # far below the theoretical break-even-clearing ceiling, so a
    # recommendation has room to still work if fair value was slightly
    # optimistic.
    conservative_buy_margin: float = 0.04

    # ---- Trend gating -------------------------------------------------
    # Trend states that block a buy-side signal outright regardless of how
    # large the apparent discount is. This is the falling-knife guard: a
    # card 25% below a 14-day-stale median in an unresolved downtrend is
    # not a bargain, it is a card that is still falling.
    trend_states_blocking_buy: tuple = ("falling_knife", "downtrend")
    # In these states a buy is still possible but is capped at "buy" -
    # never promoted to "strong_buy" - because the discount is not yet
    # confirmed to have stopped widening.
    trend_states_capping_signal: tuple = ("stabilising", "insufficient_trend_data")

    # ---- Expiry -------------------------------------------------------
    # Base shelf-life of a recommendation before market conditions are
    # assumed to have moved on, scaled down by volatility/velocity and up
    # by liquidity/confidence in expiry.py.
    base_expiry_minutes: int = 45
    min_expiry_minutes: int = 5
    max_expiry_minutes: int = 360
    # A recommendation is invalidated (not merely expired) once the live
    # BIN moves this far from the BIN it was computed against.
    invalidation_bin_drift_pct: float = 0.05

    def max_price_age_for_tier(self, price_tier: Optional[str]) -> int:
        if price_tier is None:
            return self.default_max_price_age_minutes
        return self.max_price_age_minutes_by_tier.get(
            price_tier, self.default_max_price_age_minutes
        )

    def as_dict(self) -> Dict[str, Any]:
        """JSON-safe snapshot of the whole configuration, for embedding in
        a recommendation row so the exact numbers behind a call are
        recoverable later even if the code has since moved on."""
        out: Dict[str, Any] = {}
        for key, value in asdict(self).items():
            if isinstance(value, Decimal):
                out[key] = float(value)
            elif isinstance(value, tuple):
                out[key] = list(value)
            elif isinstance(value, dict):
                out[key] = dict(value)
            else:
                out[key] = value
        return out


@dataclass(frozen=True)
class TrendConfig:
    """Thresholds for the trend/falling-knife layer. Versioned separately
    from EngineConfig - see module docstring."""

    version: str = TREND_VERSION

    # Minimum raw sales observations before any trend state other than
    # insufficient_trend_data can be returned. Below this, a "slope" is
    # noise dressed up as a signal.
    min_observations: int = 6
    # Minimum wall-clock span the observations must cover. Six sales in
    # four minutes says nothing about a trend.
    min_span_minutes: float = 90.0

    # Fractional change thresholds. Deliberately expressed against the
    # card's own price level (all the change features are ratios), so a
    # single set of thresholds works across bronze and special tiers.
    downtrend_change_threshold: float = -0.06
    uptrend_change_threshold: float = 0.06
    sideways_band: float = 0.03

    # A falling knife is a *steep, recent, unresolved* decline - all three
    # of: short-term change below this, a majority of recent sales under
    # the earlier median, and no stabilisation evidence.
    falling_knife_short_change: float = -0.10
    falling_knife_below_prior_median_ratio: float = 0.65
    falling_knife_min_drawdown: float = 0.12

    # Stabilisation: the most recent slice has stopped making new lows and
    # its dispersion has tightened relative to the earlier slice.
    stabilising_max_recent_change: float = 0.025
    stabilising_max_new_low_ratio: float = 0.20

    # Recovery: price fell meaningfully below its window high and has
    # since turned back up off the low.
    #
    # Note this is deliberately NOT gated on the window's start-to-end
    # change being negative. A clean V-shaped recovery ends near where it
    # began, so an "overall window still down" test classifies the single
    # most recognisable recovery shape as sideways. What actually
    # separates a recovery from an uptrend is that a recovery is still
    # trading below its earlier peak (drawdown_from_high > 0) whereas an
    # uptrend's latest print IS the peak.
    recovering_min_bounce: float = 0.05
    recovering_min_drawdown: float = 0.05

    # Fraction of the window treated as "recent" for short-term features.
    # 0.4 => last 40% of observations by time.
    recent_fraction: float = 0.4

    def as_dict(self) -> Dict[str, Any]:
        return {k: (list(v) if isinstance(v, tuple) else v) for k, v in asdict(self).items()}


# =============================================================================
# Active configuration
# =============================================================================
#
# Env-var overrides exist so a threshold can be moved in staging without a
# deploy, but the *default* remains the documented value above - an
# unset environment must always produce the versioned, reviewed config.
def _load_engine_config() -> EngineConfig:
    return EngineConfig(
        version=os.getenv("FUTGG_ENGINE_VERSION", ENGINE_VERSION),
        min_sales_for_signal=_env_int("FUTGG_MIN_SALES_FOR_SIGNAL", 5),
        extreme_dispersion_ratio=_env_float("FUTGG_EXTREME_DISPERSION_RATIO", 0.45),
        min_confidence_for_buy_signal=_env_float("FUTGG_MIN_CONFIDENCE_FOR_BUY", 0.45),
        min_confidence_for_any_signal=_env_float("FUTGG_MIN_CONFIDENCE_ANY", 0.20),
        min_net_roi_for_buy=_env_decimal("FUTGG_MIN_NET_ROI_FOR_BUY", "0.03"),
        min_net_roi_for_strong_buy=_env_decimal("FUTGG_MIN_NET_ROI_STRONG_BUY", "0.08"),
        fair_value_sales_weight=_env_float("FUTGG_FAIR_VALUE_SALES_WEIGHT", 0.7),
        fair_value_bin_weight=_env_float("FUTGG_FAIR_VALUE_BIN_WEIGHT", 0.3),
        conservative_buy_margin=_env_float("FUTGG_CONSERVATIVE_BUY_MARGIN", 0.04),
        base_expiry_minutes=_env_int("FUTGG_BASE_EXPIRY_MINUTES", 45),
        invalidation_bin_drift_pct=_env_float("FUTGG_INVALIDATION_DRIFT_PCT", 0.05),
    )


ENGINE_CONFIG = _load_engine_config()
TREND_CONFIG = TrendConfig()
