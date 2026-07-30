"""
Tests for app/services/trading_math.py - the central deterministic
trading math for Recommendation Engine V1.2.

The single most important test in this file is
test_tax_loss_case_is_never_positive_roi / test_break_even_example: it
directly reproduces and asserts against the bug this whole engine exists
to fix (recommendation_engine.py previously could recommend BUY on a
19,500 -> 20,000 trade, which nets a LOSS after EA's 5% tax).
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.services import trading_math as tm


# =============================================================================
# Tax / ROI / break-even - the core bug fix
# =============================================================================

class TestTaxAndRoi:
    def test_net_sale_proceeds_applies_five_percent_tax(self):
        assert tm.net_sale_proceeds(20000) == Decimal("19000.00")

    def test_tax_loss_case_19500_buy_20000_sell_is_a_net_loss(self):
        # The exact scenario from the bug report: a 500-coin nominal gap
        # is actually a loss once tax is applied.
        profit = tm.net_profit(sale_price=20000, entry_price=19500)
        assert profit == Decimal("-500.00")
        assert profit < 0

    def test_tax_loss_case_net_roi_is_negative(self):
        roi = tm.net_roi(sale_price=20000, entry_price=19500)
        assert roi is not None
        assert roi < 0

    def test_no_buy_can_result_from_the_tax_loss_case(self):
        # Any strategy policy gating on "likely_net_roi >= some positive
        # minimum" must reject this case - assert the raw number a
        # decision engine would gate on is unambiguously non-positive.
        roi = tm.net_roi(sale_price=20000, entry_price=19500)
        assert roi <= 0

    def test_net_roi_zero_entry_price_is_unavailable_not_zero(self):
        assert tm.net_roi(sale_price=1000, entry_price=0) is None

    def test_break_even_is_rounded_upward(self):
        # 19500 / 0.95 = 20526.315... -> bracket <=50000 uses 250
        # increment -> ceil(20526.315/250)=83 -> 83*250 = 20750
        be = tm.break_even_sale_price(19500)
        assert be == Decimal("20750")
        # Confirm it actually breaks even (net_profit >= 0 at this price)
        assert tm.net_profit(be, 19500) >= 0
        # And confirm the increment just below it does NOT break even
        one_increment_below = be - Decimal("250")
        assert tm.net_profit(one_increment_below, 19500) < 0

    def test_strategy_target_price_clears_tax_and_margin(self):
        # entry 19500, minimum_net_roi 0.03 (quick_flip default)
        target = tm.strategy_target_price(19500, Decimal("0.03"))
        roi = tm.net_roi(target, 19500)
        assert roi is not None
        assert roi >= Decimal("0.03")
        # One increment below should fail to clear the minimum
        below = target - tm._increment_for(target)
        roi_below = tm.net_roi(below, 19500)
        assert roi_below is None or roi_below < Decimal("0.03")


# =============================================================================
# EA increment rounding
# =============================================================================

class TestIncrementRounding:
    @pytest.mark.parametrize(
        "price,expected_increment",
        [
            (500, Decimal("50")),
            (1000, Decimal("50")),
            (1001, Decimal("100")),
            (10000, Decimal("100")),
            (10001, Decimal("250")),
            (50000, Decimal("250")),
            (50001, Decimal("500")),
            (100000, Decimal("500")),
            (100001, Decimal("1000")),
            (200000, Decimal("1000")),
            (200001, Decimal("5000")),
            (1000000, Decimal("5000")),
            (1000001, Decimal("10000")),
            (5000000, Decimal("10000")),
        ],
    )
    def test_increment_for_breakpoint(self, price, expected_increment):
        assert tm._increment_for(Decimal(price)) == expected_increment

    def test_round_up(self):
        assert tm.round_to_ea_increment(20001, "up") == Decimal("20250")

    def test_round_down(self):
        assert tm.round_to_ea_increment(20499, "down") == Decimal("20250")

    def test_round_nearest(self):
        assert tm.round_to_ea_increment(20126, "nearest") == Decimal("20250")
        assert tm.round_to_ea_increment(20124, "nearest") == Decimal("20000")

    def test_clamps_below_minimum(self):
        assert tm.round_to_ea_increment(50, "nearest") == tm.EA_MIN_PRICE

    def test_clamps_above_maximum(self):
        assert tm.round_to_ea_increment(9_999_999, "up") == tm.EA_MAX_PRICE

    def test_invalid_direction_raises(self):
        with pytest.raises(ValueError):
            tm.round_to_ea_increment(1000, "sideways")


# =============================================================================
# Percentiles / scenario prices
# =============================================================================

class TestPercentiles:
    def test_p50_of_odd_sample(self):
        assert tm.percentile([Decimal(v) for v in [10, 20, 30]], Decimal("0.5")) == Decimal("20")

    def test_p25_p50_p80_interpolation(self):
        values = [Decimal(v) for v in [100, 200, 300, 400, 500, 600, 700, 800]]
        # percentile_cont-style linear interpolation, matches Postgres
        assert tm.percentile(values, Decimal("0.25")) == Decimal("275")
        assert tm.percentile(values, Decimal("0.50")) == Decimal("450")
        assert tm.percentile(values, Decimal("0.80")) == Decimal("660")

    def test_single_value_sample(self):
        assert tm.percentile([Decimal("500")], Decimal("0.5")) == Decimal("500")

    def test_empty_sample_is_none(self):
        assert tm.percentile([], Decimal("0.5")) is None


class TestHistoricalFractionAtOrAboveLikely:
    # Retained only as a rule-calibration diagnostic per the function's
    # own docstring - not a forward probability - but it's the one
    # function in this module with no prior test coverage, so a bad edit
    # here (e.g. flipping >= to >) would have shipped silently.
    def test_no_sales_is_none(self):
        assert tm.historical_fraction_at_or_above_likely([], Decimal("1000")) is None

    def test_counts_values_at_or_above_threshold_inclusive(self):
        sales = [900, 1000, 1000, 1100, 1200]
        assert tm.historical_fraction_at_or_above_likely(sales, Decimal("1000")) == 4 / 5

    def test_all_below_threshold_is_zero(self):
        assert tm.historical_fraction_at_or_above_likely([100, 200], Decimal("1000")) == 0.0

    def test_all_at_or_above_threshold_is_one(self):
        assert tm.historical_fraction_at_or_above_likely([1000, 2000], Decimal("1000")) == 1.0


class TestScenarioPrices:
    def _sales(self, n, base=1000, step=10):
        return [base + i * step for i in range(n)]

    def test_uses_24h_when_sufficient(self):
        sales_24h = self._sales(10)
        result = tm.scenario_prices(sales_24h, [], fair_value_24h=1050, fair_value_7d=1040)
        assert result is not None
        assert result.sales_window == "24h"
        assert result.sales_sample_size == 10

    def test_falls_back_to_7d_when_24h_too_thin(self):
        sales_24h = self._sales(3)
        sales_7d = self._sales(10)
        result = tm.scenario_prices(sales_24h, sales_7d, fair_value_24h=1050, fair_value_7d=1040)
        assert result is not None
        assert result.sales_window == "7d"

    def test_insufficient_data_when_both_too_thin(self):
        result = tm.scenario_prices(self._sales(2), self._sales(3), fair_value_24h=1000, fair_value_7d=1000)
        assert result is None

    def test_insufficient_data_when_fair_value_missing(self):
        result = tm.scenario_prices(self._sales(10), [], fair_value_24h=None, fair_value_7d=1000)
        assert result is None

    def test_potential_price_is_blend(self):
        result = tm.scenario_prices(self._sales(10), [], fair_value_24h=1000, fair_value_7d=2000)
        assert result is not None
        # 0.6*1000 + 0.4*2000 = 1400
        assert result.potential_price == Decimal("1400")


# =============================================================================
# Momentum
# =============================================================================

class TestMomentum:
    def _series(self, prices, start=None, hours_apart=4):
        start = start or datetime(2026, 1, 1, tzinfo=timezone.utc)
        return [(start + timedelta(hours=i * hours_apart), p) for i, p in enumerate(prices)]

    def test_steadily_rising_price_is_positive(self):
        series = self._series([1000, 1050, 1100, 1150, 1200])
        score = tm.momentum_score(series, current_lowest_bin=1200, trend_falling=False)
        assert score is not None
        assert score > 0

    def test_steadily_falling_price_is_negative(self):
        series = self._series([1200, 1150, 1100, 1050, 1000])
        score = tm.momentum_score(series, current_lowest_bin=1000, trend_falling=False)
        assert score is not None
        assert score < 0

    def test_flat_price_is_near_zero(self):
        series = self._series([1000, 1000, 1000, 1000, 1000])
        score = tm.momentum_score(series, current_lowest_bin=1000, trend_falling=False)
        assert score is not None
        assert abs(score) < 0.01

    def test_insufficient_observations_is_none(self):
        series = self._series([1000, 1050])  # below MOMENTUM_MIN_OBSERVATIONS
        score = tm.momentum_score(series, current_lowest_bin=1050, trend_falling=False)
        assert score is None

    def test_insufficient_span_is_none(self):
        # Enough observations but all within a couple minutes - span too short
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        series = [(start + timedelta(minutes=i), 1000 + i) for i in range(5)]
        score = tm.momentum_score(series, current_lowest_bin=1004, trend_falling=False)
        assert score is None

    def test_trend_falling_override_caps_positive_momentum(self):
        # Even a rising series must be forced to a negative reading when
        # trend_falling is set - the whole point of that flag.
        series = self._series([1000, 1050, 1100, 1150, 1200])
        score = tm.momentum_score(series, current_lowest_bin=1200, trend_falling=True)
        assert score is not None
        assert score <= -0.1

    def test_missing_current_bin_is_none(self):
        series = self._series([1000, 1050, 1100, 1150])
        assert tm.momentum_score(series, current_lowest_bin=None, trend_falling=False) is None

    def test_empty_history_is_none(self):
        assert tm.momentum_score([], current_lowest_bin=1000, trend_falling=False) is None


# =============================================================================
# Liquidity
# =============================================================================

class TestLiquidity:
    def test_zero_sales_is_real_zero_not_unavailable(self):
        score = tm.liquidity_score(sales_per_hour=0, sales_count_24h=0, sales_count_7d=0)
        assert score == 0.0

    def test_missing_sales_count_is_unavailable(self):
        assert tm.liquidity_score(sales_per_hour=1.0, sales_count_24h=None, sales_count_7d=10) is None

    def test_accelerating_demand_increases_score(self):
        # sales_per_hour=0.5 keeps hourly_component well under the [0,1]
        # ceiling (unlike 2.0, which equals LIQUIDITY_REFERENCE_RATE and
        # saturates hourly_component to exactly 1.0 on its own, hiding
        # any acceleration difference behind the clamp) so the
        # acceleration_component's effect is actually visible here.
        accelerating = tm.liquidity_score(sales_per_hour=0.5, sales_count_24h=48, sales_count_7d=100)
        steady = tm.liquidity_score(sales_per_hour=0.5, sales_count_24h=48, sales_count_7d=336)  # 48/24 == 336/168
        assert accelerating is not None and steady is not None
        assert accelerating > steady

    def test_decelerating_demand_decreases_score(self):
        decelerating = tm.liquidity_score(sales_per_hour=0.5, sales_count_24h=12, sales_count_7d=336)
        steady = tm.liquidity_score(sales_per_hour=0.5, sales_count_24h=12, sales_count_7d=84)  # 12/24 == 84/168
        assert decelerating is not None and steady is not None
        assert decelerating < steady

    def test_score_bounded_zero_to_one(self):
        score = tm.liquidity_score(sales_per_hour=1000.0, sales_count_24h=5000, sales_count_7d=1)
        assert score is not None
        assert 0.0 <= score <= 1.0


# =============================================================================
# Confidence
# =============================================================================

class TestConfidence:
    def test_weights_sum_to_one(self):
        total = (
            tm.CONFIDENCE_SAMPLE_WEIGHT
            + tm.CONFIDENCE_FRESHNESS_WEIGHT
            + tm.CONFIDENCE_CONSISTENCY_WEIGHT
        )
        assert abs(total - 1.0) < 1e-9

    def test_full_sample_fresh_price_consistent_is_high(self):
        score = tm.confidence_score(
            sales_count_24h=20, sales_count_7d=140, price_age_minutes=1, bin_zscore_24h=0.0
        )
        assert score is not None
        assert score > 0.9

    def test_stale_price_reduces_confidence(self):
        fresh = tm.confidence_score(sales_count_24h=20, sales_count_7d=140, price_age_minutes=1, bin_zscore_24h=0.0)
        stale = tm.confidence_score(sales_count_24h=20, sales_count_7d=140, price_age_minutes=120, bin_zscore_24h=0.0)
        assert fresh is not None and stale is not None
        assert stale < fresh

    def test_low_sample_reduces_confidence(self):
        low_sample = tm.confidence_score(sales_count_24h=1, sales_count_7d=7, price_age_minutes=1, bin_zscore_24h=0.0)
        high_sample = tm.confidence_score(sales_count_24h=20, sales_count_7d=140, price_age_minutes=1, bin_zscore_24h=0.0)
        assert low_sample is not None and high_sample is not None
        assert low_sample < high_sample

    def test_extreme_zscore_reduces_confidence(self):
        normal = tm.confidence_score(sales_count_24h=20, sales_count_7d=140, price_age_minutes=1, bin_zscore_24h=0.0)
        extreme = tm.confidence_score(sales_count_24h=20, sales_count_7d=140, price_age_minutes=1, bin_zscore_24h=6.0)
        assert normal is not None and extreme is not None
        assert extreme < normal

    def test_missing_input_is_unavailable(self):
        assert tm.confidence_score(None, 140, 1, 0.0) is None

    def test_zero_component_zeroes_whole_score_not_just_penalizes(self):
        # price_age_minutes far beyond staleness cap -> freshness_component
        # clamps to exactly 0 -> geometric blend must be 0, not "mostly ok".
        score = tm.confidence_score(
            sales_count_24h=20, sales_count_7d=140, price_age_minutes=10_000, bin_zscore_24h=0.0
        )
        assert score == 0.0


# =============================================================================
# Risk
# =============================================================================

class TestRisk:
    def test_downside_volatility_used_when_enough_observations(self):
        # 4 downside deltas -> above RISK_MIN_DOWNSIDE_OBSERVATIONS (3)
        series = [1000, 990, 1000, 985, 1000, 970, 1000, 960]
        score = tm.risk_score(series, current_lowest_bin=960, volatility_24h=None, price_age_minutes=1)
        assert score is not None
        assert 0.0 <= score <= 1.0

    def test_falls_back_to_volatility_24h_when_insufficient_downside_observations(self):
        series = [1000, 1000]  # zero downside deltas
        score = tm.risk_score(series, current_lowest_bin=1000, volatility_24h=50, price_age_minutes=1)
        assert score is not None

    def test_unavailable_when_no_downside_history_and_no_fallback(self):
        series = [1000, 1000]
        score = tm.risk_score(series, current_lowest_bin=1000, volatility_24h=None, price_age_minutes=1)
        assert score is None

    def test_stale_price_increases_risk(self):
        series = [1000, 990, 1000, 985, 1000, 970]
        fresh = tm.risk_score(series, current_lowest_bin=970, volatility_24h=None, price_age_minutes=1)
        stale = tm.risk_score(series, current_lowest_bin=970, volatility_24h=None, price_age_minutes=120)
        assert fresh is not None and stale is not None
        assert stale > fresh

    def test_missing_current_bin_is_unavailable(self):
        assert tm.risk_score([1000, 990], current_lowest_bin=None, volatility_24h=10, price_age_minutes=1) is None


# =============================================================================
# Valuation
# =============================================================================

class TestValuation:
    def test_cheap_entry_is_positive(self):
        score = tm.valuation_score(
            fair_value_24h=1000, entry_price=800, likely_price=950, bin_zscore_24h=-1.5
        )
        assert score is not None
        assert score > 0

    def test_expensive_entry_is_negative(self):
        score = tm.valuation_score(
            fair_value_24h=1000, entry_price=1200, likely_price=1000, bin_zscore_24h=1.5
        )
        assert score is not None
        assert score < 0

    def test_missing_input_is_unavailable(self):
        assert tm.valuation_score(None, 800, 950, -1.5) is None

    def test_bounded_within_range(self):
        score = tm.valuation_score(fair_value_24h=1000, entry_price=1, likely_price=1, bin_zscore_24h=-100)
        assert score is not None
        assert -1.0 <= score <= 1.0
