# app/services/futgg_intelligence.py
"""
Fair value / recommendation logic for the FUT.GG-backed market layer.

WHAT THIS ENGINE IS
-------------------
A mispricing detector, not a forecaster. It asks one question - "is the
lowest current BIN below what this card has recently been selling for,
by enough to clear EA's 5% tax with margin to spare" - and surrounds that
question with the evidence needed to decide whether the answer can be
trusted. It does not predict where a price is going, and no user-facing
label should imply that it does.

WHAT CHANGED IN ENGINE v2 (see futgg_config.ENGINE_VERSION)
-----------------------------------------------------------
Three structural corrections, each of which changes real recommendations:

  1. TREND GATING. The engine previously had no trend term whatsoever.
     Because a falling card's sales median goes stale high, the further a
     card had fallen the *larger* its apparent discount - so unresolved
     downtrends were systematically surfaced as the strongest buys. The
     trend layer (futgg_trend.py, separately versioned) now gates buy
     signals: a card in a falling-knife or downtrend state cannot be a
     buy on the strength of a discount to a stale median alone.

  2. SEPARATED BUY PRICES. `recommended_buy_max` used to be clamped to
     `current_bin`, which made the recommendation circular: the "maximum
     you should pay" became "whatever it currently costs", so a card was
     described as a valid buy purely because its own asking price had
     been substituted into the calculation. There are now four distinct,
     independently meaningful prices - theoretical maximum, recommended
     (conservative) maximum, currently executable entry, and break-even -
     and when the live BIN sits above the recommended maximum the result
     is a WATCH with an explicit trigger price, not a buy.

  3. STRUCTURED REASONS + VERSIONING. Every rejection or downgrade now
     carries a stable machine-readable code (futgg_reasons.py) alongside
     its English, and every result is stamped with the engine and trend
     versions - so the outcome grader can attribute a result to a
     specific configuration rather than "whatever the code said at the
     time".

DATA SEMANTICS
--------------
Inputs are FUT.GG's, which differ from the legacy FUTBIN fair_value_mv
path in two structural ways:

  1. There is no separate "24h vs 7d" sales split - futgg_sales_history
     gives a bounded recent window (<=50 rows / 14 days, see migration
     038). Confidence/sample-size scoring therefore works off a single
     window's count plus the actual time span it covers (50 sales in 20
     minutes is a very different liquidity signal from 50 sales over 14
     days, though both are "n=50").

  2. approximate_sold_at is structurally never exact (see
     futgg_sales_history's column comment), so sale timing is treated as
     an approximate freshness/ordering signal, never an exact timestamp.

All tax/ROI/EA-increment math is delegated to app.services.trading_math -
nothing here hand-rolls `* 0.95` or a break-even formula.

evaluate_card() is pure (dict in, dataclass out) and fully unit-testable
with constructed snapshot dicts - no I/O. The I/O (reading
futgg_market_snapshot and futgg_sales_history) lives in
market_data_provider.py; the router layer wires the two together.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence

from app.services import trading_math as tm
from app.services import futgg_reasons as rc
from app.services.futgg_config import ENGINE_CONFIG, EngineConfig, TREND_CONFIG
from app.services.futgg_reasons import ReasonList
from app.services.futgg_trend import (
    INSUFFICIENT_TREND_DATA, TrendAssessment, evaluate_trend,
)

# =============================================================================
# Backwards-compatible constant surface
# =============================================================================
# These names are imported by app/routers/v2/futgg_market.py and the
# existing test-suite. They now read through to the versioned config
# rather than being independent literals, so a threshold is defined in
# exactly one place - but the names stay put so this refactor doesn't
# ripple into unrelated call sites.
MAX_ACCEPTABLE_PRICE_AGE_MINUTES_BY_TIER = dict(ENGINE_CONFIG.max_price_age_minutes_by_tier)
DEFAULT_MAX_ACCEPTABLE_PRICE_AGE_MINUTES = ENGINE_CONFIG.default_max_price_age_minutes
MIN_SALES_FOR_SIGNAL = ENGINE_CONFIG.min_sales_for_signal
EXTREME_DISPERSION_RATIO = ENGINE_CONFIG.extreme_dispersion_ratio
MIN_CONFIDENCE_FOR_BUY_SIGNAL = ENGINE_CONFIG.min_confidence_for_buy_signal
MIN_CONFIDENCE_FOR_ANY_SIGNAL = ENGINE_CONFIG.min_confidence_for_any_signal
MIN_NET_ROI_FOR_BUY = ENGINE_CONFIG.min_net_roi_for_buy
MIN_NET_ROI_FOR_STRONG_BUY = ENGINE_CONFIG.min_net_roi_for_strong_buy
FAIR_VALUE_SALES_WEIGHT = ENGINE_CONFIG.fair_value_sales_weight
FAIR_VALUE_BIN_WEIGHT = ENGINE_CONFIG.fair_value_bin_weight

RISK_LEVELS = ("low", "medium", "high", "avoid")
SIGNALS = ("strong_buy", "buy", "watch", "hold", "sell", "avoid", "insufficient_data")

# Lifecycle status, distinct from `signal`. `signal` is what the engine
# concluded; `status` is whether that conclusion is still usable. Only
# ACTIVE/WATCH/INSUFFICIENT_DATA are reachable at evaluation time -
# EXPIRED and INVALIDATED are assigned later by re-checking a persisted
# recommendation against the live market (recommendation_lifecycle.py).
STATUS_ACTIVE = "active"
STATUS_WATCH = "watch"
STATUS_EXPIRED = "expired"
STATUS_INVALIDATED = "invalidated"
STATUS_INSUFFICIENT_DATA = "insufficient_data"
STATUSES = (STATUS_ACTIVE, STATUS_WATCH, STATUS_EXPIRED, STATUS_INVALIDATED, STATUS_INSUFFICIENT_DATA)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass
class CardIntelligence:
    card_id: int

    # ---- Valuation ----------------------------------------------------
    fair_value: Optional[Decimal] = None

    # ---- The four distinct prices (see module docstring, point 2) -----
    # The highest entry that could theoretically still clear the minimum
    # ROI bar. Derived purely from fair value - it knows nothing about
    # today's ask, and is NOT a price we advise paying.
    theoretical_max_buy: Optional[Decimal] = None
    # What we actually advise as a ceiling: the theoretical maximum with a
    # margin removed so the trade still works if fair value was a little
    # optimistic. This is the number a user should act on.
    recommended_buy_max: Optional[Decimal] = None
    # The live ask, but only when it is at or below recommended_buy_max.
    # None means "not currently buyable at a sensible price" - precisely
    # the watch case, and why this is a separate field rather than being
    # clamped into recommended_buy_max.
    current_executable_buy: Optional[Decimal] = None
    # Minimum sale price needed to recover the entry after EA's tax.
    break_even_price: Optional[Decimal] = None
    recommended_sell_target: Optional[Decimal] = None

    # Populated on a WATCH: the price the card must fall to before this
    # becomes an actionable buy.
    buy_below: Optional[Decimal] = None

    # ---- Expected outcome (always computed against the price a user
    # would actually pay, never against an unshown intermediate) --------
    expected_profit_after_tax: Optional[Decimal] = None
    expected_roi: Optional[Decimal] = None

    # ---- Evidence -----------------------------------------------------
    liquidity_score: Optional[float] = None
    confidence_score: Optional[float] = None
    risk_level: str = "high"
    signal: str = "insufficient_data"
    status: str = STATUS_INSUFFICIENT_DATA
    signal_reasons: List[str] = field(default_factory=list)
    reason_codes: List[str] = field(default_factory=list)
    reasons: List[Dict[str, str]] = field(default_factory=list)
    blocking_codes: List[str] = field(default_factory=list)

    price_age_minutes: Optional[int] = None
    sales_sample_size: int = 0
    sales_window_span_minutes: Optional[float] = None

    # ---- Trend --------------------------------------------------------
    trend_state: str = INSUFFICIENT_TREND_DATA
    trend_description: str = ""
    trend_features: Dict[str, Any] = field(default_factory=dict)

    # ---- Provenance / lifecycle ---------------------------------------
    engine_version: str = ENGINE_CONFIG.version
    trend_version: str = TREND_CONFIG.version
    evaluated_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    expiry_minutes: Optional[int] = None

    # The BIN this recommendation was computed against. Persisted so a
    # later lifecycle check can detect that the market has moved away from
    # the state that produced the call and invalidate it, rather than keep
    # showing a profit figure derived from a price that no longer exists.
    evaluated_bin: Optional[int] = None


def _price_age_minutes(bin_captured_at: Optional[datetime], as_of: datetime) -> Optional[int]:
    if bin_captured_at is None:
        return None
    if bin_captured_at.tzinfo is None:
        bin_captured_at = bin_captured_at.replace(tzinfo=timezone.utc)
    return max(0, int((as_of - bin_captured_at).total_seconds() // 60))


def _sales_age_minutes(latest_sale_at: Optional[datetime], as_of: datetime) -> Optional[int]:
    """Age of the NEWEST completed sale.

    Distinct from price age and just as important: fair value is 70%
    sales-derived, so a fresh BIN sitting beside a days-old sales sample
    produces a confident verdict about a market that has already moved.
    """
    if latest_sale_at is None:
        return None
    if latest_sale_at.tzinfo is None:
        latest_sale_at = latest_sale_at.replace(tzinfo=timezone.utc)
    return max(0, int((as_of - latest_sale_at).total_seconds() // 60))


def _compute_liquidity_score(sales_count: int, span_minutes: Optional[float]) -> Optional[float]:
    """None only when the window itself is meaningless. A real 0 sales
    count IS scored as 0.0 - a genuinely illiquid card, not "unknown"."""
    if sales_count == 0:
        return 0.0
    if not span_minutes or span_minutes <= 0:
        span_minutes = 60.0
    sales_per_hour = sales_count / (span_minutes / 60.0)
    return tm.liquidity_score(sales_per_hour, sales_count, sales_count * 7)


def _compute_confidence(
    *, sales_count: int, price_age_minutes: Optional[int],
    dispersion_ratio: Optional[float], is_tradeable: Optional[bool],
    max_acceptable_price_age_minutes: int, config: EngineConfig,
    sales_age_minutes: Optional[int] = None,
) -> float:
    """Geometric-blend confidence in [0, 1] - "one bad component tanks the
    whole score", same philosophy as trading_math.confidence_score,
    reimplemented here because FUT.GG's inputs don't map onto that
    function's parameter set (no 24h/7d split, dispersion ratio rather
    than a BIN z-score)."""
    if is_tradeable is False:
        return 0.0

    sample_component = min(sales_count, 20) / 20.0
    if price_age_minutes is None:
        freshness_component = 0.0
    else:
        freshness_component = 1.0 - _clamp(price_age_minutes / max_acceptable_price_age_minutes, 0.0, 1.0)
    if dispersion_ratio is None:
        dispersion_component = 0.5  # unknown - neutral, neither rewarded nor punished
    else:
        dispersion_component = 1.0 - _clamp(dispersion_ratio / config.extreme_dispersion_ratio, 0.0, 1.0)

    # Sales freshness. Unknown (no latest_sale_at) is neutral rather than
    # punished - plenty of legitimately thin cards simply have no recent
    # print - but a KNOWN-old sample decays toward zero, because a median
    # from days ago is not evidence about today's price.
    if sales_age_minutes is None:
        sales_freshness_component = 0.6
    else:
        sales_freshness_component = 1.0 - _clamp(
            sales_age_minutes / max(config.reject_sales_older_than_minutes, 1), 0.0, 1.0
        )

    components = (
        (max(sample_component, 0.0), 0.30),
        (max(freshness_component, 0.0), 0.28),
        (max(dispersion_component, 0.0), 0.20),
        (max(sales_freshness_component, 0.0), 0.22),
    )
    if any(base == 0.0 for base, _ in components):
        return 0.0
    return _clamp(math.exp(sum(w * math.log(b) for b, w in components)), 0.0, 1.0)


def _compute_risk_level(
    *, confidence: float, dispersion_ratio: Optional[float],
    price_age_minutes: Optional[int], sales_count: int,
    is_tradeable: Optional[bool], max_acceptable_price_age_minutes: int,
    trend_state: str, config: EngineConfig,
) -> str:
    if is_tradeable is False:
        return "avoid"
    if (
        sales_count < config.min_sales_for_signal
        or price_age_minutes is None
        or price_age_minutes > max_acceptable_price_age_minutes
    ):
        return "high"
    if dispersion_ratio is not None and dispersion_ratio >= config.extreme_dispersion_ratio:
        return "high"
    # A card still in an unresolved decline is high risk regardless of how
    # tidy its other statistics look - those statistics are describing a
    # price level the market has already left.
    if trend_state in config.trend_states_blocking_buy:
        return "high"
    if confidence >= 0.65 and (dispersion_ratio is None or dispersion_ratio < 0.20):
        return "low"
    return "medium"


def _compute_expiry_minutes(
    *, confidence: float, liquidity: Optional[float],
    dispersion_ratio: Optional[float], price_age_minutes: Optional[int],
    max_acceptable_price_age_minutes: int, distance_from_threshold: Optional[float],
    config: EngineConfig,
) -> int:
    """How long this recommendation stays credible.

    A recommendation cannot be valid indefinitely - the market state it
    describes decays. Shelf life shortens with volatility, trading
    velocity and an already-old price observation, and lengthens with
    confidence and a comfortable margin over the buy threshold (a call
    that only just clears the bar stops being true after a small move;
    one clearing it by a mile survives a larger one).
    """
    minutes = float(config.base_expiry_minutes)

    if dispersion_ratio is not None:
        minutes *= _clamp(1.0 - (dispersion_ratio / config.extreme_dispersion_ratio) * 0.6, 0.3, 1.0)
    if liquidity is not None:
        # A fast-trading card re-prices sooner than a slow one.
        minutes *= _clamp(1.15 - liquidity * 0.5, 0.5, 1.15)
    minutes *= _clamp(0.7 + confidence * 0.6, 0.7, 1.3)
    if distance_from_threshold is not None:
        # A wafer-thin edge dies on the first tick.
        minutes *= _clamp(0.6 + distance_from_threshold * 4.0, 0.6, 1.4)

    # Whatever freshness budget the price observation has already spent is
    # gone - a 40-minute-old price on a 45-minute tier has ~5 minutes of
    # credibility left, not a fresh 45.
    if price_age_minutes is not None:
        remaining = max_acceptable_price_age_minutes - price_age_minutes
        minutes = min(minutes, float(remaining)) if remaining > 0 else float(config.min_expiry_minutes)

    return int(_clamp(minutes, config.min_expiry_minutes, config.max_expiry_minutes))


def _insufficient(
    card_id: int, reasons: ReasonList, *, as_of: datetime, config: EngineConfig,
    confidence: float = 0.0, liquidity: Optional[float] = None,
    risk_level: str = "high", price_age_minutes: Optional[int] = None,
    sales_count: int = 0, span_minutes: Optional[float] = None,
    trend: Optional[TrendAssessment] = None,
    signal: str = "insufficient_data", current_bin: Optional[int] = None,
) -> CardIntelligence:
    return CardIntelligence(
        card_id=card_id, fair_value=None, confidence_score=confidence,
        liquidity_score=liquidity, risk_level=risk_level, signal=signal,
        status=STATUS_INSUFFICIENT_DATA,
        signal_reasons=reasons.messages, reason_codes=reasons.codes,
        reasons=reasons.as_dicts(), blocking_codes=reasons.blocking_codes,
        price_age_minutes=price_age_minutes, sales_sample_size=sales_count,
        sales_window_span_minutes=span_minutes,
        trend_state=trend.state if trend else INSUFFICIENT_TREND_DATA,
        trend_description=trend.description if trend else "",
        trend_features=trend.features.as_dict() if trend else {},
        engine_version=config.version,
        trend_version=trend.version if trend else TREND_CONFIG.version,
        evaluated_at=as_of,
        evaluated_bin=int(current_bin) if current_bin is not None else None,
    )


def evaluate_card(
    snapshot: Dict[str, Any], *,
    as_of: Optional[datetime] = None,
    sales: Optional[Sequence[Any]] = None,
    config: Optional[EngineConfig] = None,
) -> CardIntelligence:
    """Pure function: one futgg_market_snapshot row (or an equivalent
    constructed dict) -> CardIntelligence.

    `sales` is the optional raw sales series (futgg_sales_history rows)
    used for trend assessment. When omitted the trend layer reports
    INSUFFICIENT_TREND_DATA and buy signals are capped rather than
    blocked - so a caller that cannot afford the extra query degrades to
    "cannot confirm the trend" rather than to the old, dangerous
    "trend does not exist" behaviour.

    Never raises on missing/None fields - every optional input degrades to
    a documented insufficient-data outcome.
    """
    cfg = config or ENGINE_CONFIG
    as_of = as_of or datetime.now(timezone.utc)
    card_id = snapshot["source_card_id"]
    is_tradeable = snapshot.get("is_tradeable")
    reasons = ReasonList()

    if is_tradeable is False:
        reasons.add(
            rc.UNTRADEABLE,
            "Card is untradeable (SBC/objective reward) - never a live market target.",
        )
        return _insufficient(
            card_id, reasons, as_of=as_of, risk_level="avoid",
            signal="avoid", config=cfg,
        )

    current_bin = snapshot.get("current_bin")
    bin_captured_at = snapshot.get("bin_captured_at")
    sales_count = int(snapshot.get("sales_count") or 0)
    sales_median = snapshot.get("sales_median")
    sales_trimmed_mean = snapshot.get("sales_trimmed_mean")

    # Postgres numerics arrive from asyncpg as Decimal; normalise once
    # here rather than at each arithmetic site (mixing Decimal with a
    # float literal raises TypeError).
    span_minutes = snapshot.get("sales_window_span_minutes")
    span_minutes = float(span_minutes) if span_minutes is not None else None
    dispersion_ratio = snapshot.get("sales_dispersion_ratio")
    dispersion_ratio = float(dispersion_ratio) if dispersion_ratio is not None else None

    price_age_minutes = _price_age_minutes(bin_captured_at, as_of)
    sales_age_minutes = _sales_age_minutes(snapshot.get("latest_sale_at"), as_of)
    price_tier = snapshot.get("price_tier")
    max_age = cfg.max_price_age_for_tier(price_tier)

    trend = evaluate_trend(sales or [], as_of=as_of, config=TREND_CONFIG)

    confidence = _compute_confidence(
        sales_count=sales_count, price_age_minutes=price_age_minutes,
        dispersion_ratio=dispersion_ratio, is_tradeable=is_tradeable,
        max_acceptable_price_age_minutes=max_age, config=cfg,
        sales_age_minutes=sales_age_minutes,
    )
    risk_level = _compute_risk_level(
        confidence=confidence, dispersion_ratio=dispersion_ratio,
        price_age_minutes=price_age_minutes, sales_count=sales_count,
        is_tradeable=is_tradeable, max_acceptable_price_age_minutes=max_age,
        trend_state=trend.state, config=cfg,
    )
    liquidity = _compute_liquidity_score(sales_count, span_minutes)

    # ---- Hard "cannot evaluate at all" gates ------------------------------
    if sales_count < cfg.min_sales_for_signal:
        reasons.add(
            rc.INSUFFICIENT_SALES,
            f"Only {sales_count} recent sale(s) recorded - need at least "
            f"{cfg.min_sales_for_signal} to trust a price.",
        )
    if price_age_minutes is None:
        reasons.add(rc.NO_LIVE_PRICE, "No BIN price observation recorded for this card.")
    elif price_age_minutes > max_age:
        reasons.add(
            rc.STALE_MARKET,
            f"Current price observation is {price_age_minutes} minutes old - beyond the "
            f"{max_age}-minute freshness limit for a {price_tier or 'unknown'}-tier card.",
        )
    if dispersion_ratio is not None and dispersion_ratio >= cfg.extreme_dispersion_ratio:
        reasons.add(
            rc.EXCESSIVE_DISPERSION,
            f"Recent sales dispersion ratio {dispersion_ratio:.2f} is extreme "
            f"(>= {cfg.extreme_dispersion_ratio}) - the price is too volatile to anchor a "
            "fair value on right now.",
        )
    if current_bin is None:
        reasons.add(rc.NO_LIVE_PRICE, "No live BIN listing found.")
    if (
        sales_age_minutes is not None
        and sales_age_minutes > cfg.reject_sales_older_than_minutes
    ):
        # Blocking, not merely a confidence haircut. Fair value is 70%
        # sales-derived, so past this age the number being compared
        # against the live BIN describes a market that no longer exists -
        # and presenting that comparison as a verdict is worse than
        # admitting we cannot say.
        reasons.add(
            rc.STALE_SALES,
            f"The most recent completed sale is {sales_age_minutes / 60:.0f} hours old - "
            "too old to value this card against today's price.",
        )

    sales_estimate = sales_trimmed_mean if sales_trimmed_mean is not None else sales_median
    if sales_estimate is None:
        # Should be unreachable when sales_count >= min_sales_for_signal
        # (all three come from the same grouped query), but this was once
        # an `assert` - a real 500, or a silent no-op under `python -O`,
        # if that invariant is ever violated by a row this function didn't
        # anticipate. Degrade like every other gate instead.
        reasons.add(
            rc.NO_REALISTIC_EXIT_EVIDENCE,
            "No recent-sales price estimate available for this card.",
        )

    if len(reasons):
        return _insufficient(
            card_id, reasons, as_of=as_of, confidence=confidence,
            liquidity=liquidity, risk_level=risk_level,
            price_age_minutes=price_age_minutes, sales_count=sales_count,
            span_minutes=span_minutes, trend=trend, config=cfg,
            current_bin=current_bin,
        )

    # =====================================================================
    # Valuation
    # =====================================================================
    sales_weight = Decimal(str(cfg.fair_value_sales_weight))
    bin_weight = Decimal(str(cfg.fair_value_bin_weight))
    fair_value = Decimal(str(sales_estimate)) * sales_weight + Decimal(str(current_bin)) * bin_weight

    recommended_sell_target = tm.round_to_ea_increment(fair_value, direction="down")

    # ---- The four prices, each independently meaningful ------------------
    #
    # theoretical_max_buy is trading_math's net_roi() relation solved for
    # entry price: the highest entry that still clears min_net_roi_for_buy
    # when sold at fair value after EA's tax. Crucially it is NOT clamped
    # to current_bin - clamping made the number circular (the "maximum you
    # should pay" became "what it costs"), which is exactly how a card
    # ended up described as a valid buy purely because its own ask had
    # been substituted into the calculation.
    theoretical_max_buy = tm.round_to_ea_increment(
        fair_value * (Decimal("1") - tm.EA_TAX) / (Decimal("1") + cfg.min_net_roi_for_buy),
        direction="down",
    )
    # The advised ceiling sits below the theoretical one so the trade
    # still works if fair value proves slightly optimistic.
    recommended_buy_max = tm.round_to_ea_increment(
        theoretical_max_buy * (Decimal("1") - Decimal(str(cfg.conservative_buy_margin))),
        direction="down",
    )

    current_bin_dec = Decimal(str(current_bin))
    is_executable = current_bin_dec <= recommended_buy_max
    current_executable_buy = current_bin_dec if is_executable else None
    break_even_price = tm.break_even_sale_price(current_bin_dec)

    # Expected outcome is always computed against the price a user would
    # actually pay. On a buy that is the live BIN; on a watch it is the
    # trigger price, and the UI labels it as prospective.
    entry_for_expectation = current_bin_dec if is_executable else recommended_buy_max
    expected_profit_after_tax = tm.net_profit(recommended_sell_target, entry_for_expectation)
    expected_roi = tm.net_roi(recommended_sell_target, entry_for_expectation)

    # ---- Evidence reasons (informational) --------------------------------
    if sales_median and sales_median > 0:
        gap_pct = (float(sales_median) - float(current_bin)) / float(sales_median) * 100
        direction = "below" if gap_pct > 0 else "above"
        reasons.add(
            rc.INFO_BIN_VS_MEDIAN,
            f"Current BIN is {abs(gap_pct):.1f}% {direction} the median of "
            f"{sales_count} recent sales.",
        )
    if span_minutes is not None and sales_count > 1:
        if span_minutes < 60:
            reasons.add(rc.INFO_SALES_WINDOW, f"{sales_count} sales occurred within {span_minutes:.0f} minutes.")
        else:
            reasons.add(rc.INFO_SALES_WINDOW, f"{sales_count} sales occurred over the last {span_minutes / 60.0:.1f} hours.")
    if price_age_minutes is not None:
        reasons.add(rc.INFO_PRICE_AGE, f"Current price observation is {price_age_minutes} minute(s) old.")
    if sales_age_minutes is not None:
        if sales_age_minutes >= 120:
            reasons.add(
                rc.INFO_SALES_WINDOW,
                f"Most recent completed sale is {sales_age_minutes / 60:.1f} hours old.",
            )
        else:
            reasons.add(
                rc.INFO_SALES_WINDOW,
                f"Most recent completed sale is {sales_age_minutes} minute(s) old.",
            )
    if dispersion_ratio is not None:
        reasons.add(rc.INFO_DISPERSION, f"Recent sales dispersion ratio is {dispersion_ratio:.2f}.")
    reasons.add(rc.INFO_TREND_STATE, trend.description)

    # =====================================================================
    # Signal decision
    # =====================================================================
    trend_blocks = trend.state in cfg.trend_states_blocking_buy
    trend_caps = trend.state in cfg.trend_states_capping_signal

    distance_from_threshold: Optional[float] = None
    if recommended_buy_max and recommended_buy_max > 0:
        distance_from_threshold = float((recommended_buy_max - current_bin_dec) / recommended_buy_max)

    if expected_roi is None or expected_roi <= 0:
        signal, status = "avoid", STATUS_INSUFFICIENT_DATA
        reasons.add(
            rc.TARGET_BELOW_BREAK_EVEN,
            "Expected sell target does not clear EA's 5% tax at this entry - not a real edge.",
        )
    elif trend_blocks:
        # The falling-knife guard, and the single most important gate in
        # the engine: it is what stops the largest apparent discounts -
        # which are, precisely, the cards that have fallen hardest - from
        # being ranked as the best opportunities.
        signal, status = "avoid", STATUS_INSUFFICIENT_DATA
        code = rc.FALLING_KNIFE if trend.state == "falling_knife" else rc.UNRESOLVED_DOWNTREND
        reasons.add(
            code,
            "The discount against recent sales is explained by an unresolved downtrend, "
            "not by underpricing - recent sales are still printing lower.",
        )
    elif confidence < cfg.min_confidence_for_any_signal:
        signal, status = "insufficient_data", STATUS_INSUFFICIENT_DATA
        reasons.add(
            rc.LOW_CONFIDENCE,
            f"Confidence score {confidence:.2f} is below the minimum "
            f"{cfg.min_confidence_for_any_signal} needed to act on any signal.",
        )
    elif not is_executable:
        # Item 10's core correction: above the advised ceiling is a WATCH
        # with an explicit trigger, never a buy.
        signal, status = "watch", STATUS_WATCH
        reasons.add(
            rc.PRICE_ABOVE_MAX_BUY,
            f"Currently {int(current_bin):,} - above the {int(recommended_buy_max):,} "
            "maximum that leaves a worthwhile margin.",
        )
        reasons.add(
            rc.INFO_WATCH_THRESHOLD,
            f"Buy only if the price falls to {int(recommended_buy_max):,} or below.",
        )
    elif (
        confidence >= cfg.min_confidence_for_buy_signal
        and expected_roi >= cfg.min_net_roi_for_strong_buy
        and risk_level in ("low", "medium")
        and not trend_caps
    ):
        signal, status = "strong_buy", STATUS_ACTIVE
        reasons.add(
            rc.INFO_ROI_CLEARS_STRONG_BUY,
            f"Expected net ROI {float(expected_roi) * 100:.1f}% clears the strong-buy "
            f"threshold ({float(cfg.min_net_roi_for_strong_buy) * 100:.0f}%) with confidence {confidence:.2f}.",
        )
    elif (
        confidence >= cfg.min_confidence_for_buy_signal
        and expected_roi >= cfg.min_net_roi_for_buy
        and risk_level != "high"
    ):
        signal, status = "buy", STATUS_ACTIVE
        reasons.add(
            rc.INFO_ROI_CLEARS_BUY,
            f"Expected net ROI {float(expected_roi) * 100:.1f}% clears the buy threshold "
            f"({float(cfg.min_net_roi_for_buy) * 100:.0f}%) with confidence {confidence:.2f}.",
        )
        if trend_caps:
            reasons.add(
                rc.TREND_UNCONFIRMED,
                "Held at buy rather than strong buy: the price has fallen and only "
                "recently flattened, so the discount is not yet confirmed.",
            )
    elif expected_roi > 0 and confidence >= cfg.min_confidence_for_any_signal:
        signal, status = "watch", STATUS_WATCH
        reasons.add(
            rc.EXPECTED_PROFIT_TOO_LOW,
            "Positive expected edge but below the buy-signal confidence/ROI/risk bar.",
        )
    else:
        signal, status = "hold", STATUS_WATCH
        reasons.add(rc.INFO_NO_EDGE, "No actionable edge in either direction right now.")

    buy_below = recommended_buy_max if signal in ("watch", "hold") else None

    expiry_minutes = _compute_expiry_minutes(
        confidence=confidence, liquidity=liquidity, dispersion_ratio=dispersion_ratio,
        price_age_minutes=price_age_minutes, max_acceptable_price_age_minutes=max_age,
        distance_from_threshold=distance_from_threshold, config=cfg,
    )

    return CardIntelligence(
        card_id=card_id,
        fair_value=fair_value.quantize(Decimal("1")),
        theoretical_max_buy=theoretical_max_buy,
        recommended_buy_max=recommended_buy_max,
        current_executable_buy=current_executable_buy,
        break_even_price=break_even_price,
        recommended_sell_target=recommended_sell_target,
        buy_below=buy_below,
        expected_profit_after_tax=expected_profit_after_tax,
        expected_roi=expected_roi,
        liquidity_score=liquidity,
        confidence_score=confidence,
        risk_level=risk_level,
        signal=signal,
        status=status,
        signal_reasons=reasons.messages,
        reason_codes=reasons.codes,
        reasons=reasons.as_dicts(),
        blocking_codes=reasons.blocking_codes,
        price_age_minutes=price_age_minutes,
        sales_sample_size=sales_count,
        sales_window_span_minutes=span_minutes,
        trend_state=trend.state,
        trend_description=trend.description,
        trend_features=trend.features.as_dict(),
        engine_version=cfg.version,
        trend_version=trend.version,
        evaluated_at=as_of,
        expires_at=as_of + timedelta(minutes=expiry_minutes),
        expiry_minutes=expiry_minutes,
        evaluated_bin=int(current_bin),
    )
