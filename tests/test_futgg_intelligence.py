"""
Tests for app/services/futgg_intelligence.py's pure evaluate_card() layer
- no database, no I/O. Snapshot dicts below mimic rows of the
futgg_market_snapshot materialized view (migrations/038).
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.services import trading_math as tm
from app.services.futgg_intelligence import (
    MAX_ACCEPTABLE_PRICE_AGE_MINUTES,
    MIN_SALES_FOR_SIGNAL,
    evaluate_card,
)

AS_OF = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)


def _snapshot(**overrides):
    defaults = dict(
        source_card_id=1,
        is_tradeable=True,
        current_bin=10000,
        bin_captured_at=AS_OF - timedelta(minutes=5),
        sales_count=25,
        sales_median=11000,
        sales_trimmed_mean=11000,
        sales_low=10500,
        sales_high=11500,
        sales_stddev=200,
        sales_window_span_minutes=180.0,
        sales_dispersion_ratio=0.018,
    )
    defaults.update(overrides)
    return defaults


class TestUntradeable:
    def test_untradeable_card_is_always_avoid(self):
        ci = evaluate_card(_snapshot(is_tradeable=False), as_of=AS_OF)
        assert ci.signal == "avoid"
        assert ci.risk_level == "avoid"
        assert ci.confidence_score == 0.0
        assert ci.fair_value is None
        assert any("untradeable" in r.lower() for r in ci.signal_reasons)


class TestInsufficientData:
    def test_small_sales_sample_is_insufficient_data(self):
        ci = evaluate_card(_snapshot(sales_count=MIN_SALES_FOR_SIGNAL - 1), as_of=AS_OF)
        assert ci.signal == "insufficient_data"
        assert ci.fair_value is None
        assert any("recent sale" in r.lower() for r in ci.signal_reasons)

    def test_missing_bin_is_insufficient_data(self):
        ci = evaluate_card(_snapshot(current_bin=None, bin_captured_at=None), as_of=AS_OF)
        assert ci.signal == "insufficient_data"
        assert ci.price_age_minutes is None

    def test_stale_price_is_insufficient_data(self):
        stale_at = AS_OF - timedelta(minutes=MAX_ACCEPTABLE_PRICE_AGE_MINUTES + 1)
        ci = evaluate_card(_snapshot(bin_captured_at=stale_at), as_of=AS_OF)
        assert ci.signal == "insufficient_data"
        assert any("minutes old" in r for r in ci.signal_reasons)

    def test_never_fabricates_price_from_missing_bin(self):
        """A snapshot with no BIN row must never return a fair_value or
        recommended prices - None, not 0 or a sales-only guess."""
        ci = evaluate_card(_snapshot(current_bin=None, bin_captured_at=None), as_of=AS_OF)
        assert ci.fair_value is None
        assert ci.recommended_buy_max is None
        assert ci.recommended_sell_target is None
        assert ci.expected_profit_after_tax is None
        assert ci.expected_roi is None

    def test_extreme_dispersion_is_insufficient_data(self):
        ci = evaluate_card(_snapshot(sales_dispersion_ratio=0.9), as_of=AS_OF)
        assert ci.signal == "insufficient_data"
        assert any("dispersion" in r.lower() for r in ci.signal_reasons)


class TestConfidenceGradient:
    def test_higher_dispersion_lowers_confidence(self):
        low_disp = evaluate_card(_snapshot(sales_dispersion_ratio=0.02), as_of=AS_OF)
        high_disp = evaluate_card(_snapshot(sales_dispersion_ratio=0.30), as_of=AS_OF)
        assert low_disp.confidence_score > high_disp.confidence_score

    def test_stale_but_still_acceptable_price_lowers_confidence(self):
        fresh = evaluate_card(_snapshot(bin_captured_at=AS_OF - timedelta(minutes=2)), as_of=AS_OF)
        older = evaluate_card(
            _snapshot(bin_captured_at=AS_OF - timedelta(minutes=MAX_ACCEPTABLE_PRICE_AGE_MINUTES - 5)),
            as_of=AS_OF,
        )
        assert fresh.confidence_score > older.confidence_score

    def test_larger_sample_raises_confidence(self):
        thin = evaluate_card(_snapshot(sales_count=MIN_SALES_FOR_SIGNAL), as_of=AS_OF)
        thick = evaluate_card(_snapshot(sales_count=40), as_of=AS_OF)
        assert thick.confidence_score >= thin.confidence_score


class TestTaxDelegatesToTradingMath:
    # expected_profit_after_tax/expected_roi are computed against
    # recommended_buy_max (the entry price actually shown to the user as
    # "Buy below"), not the raw current_bin - see evaluate_card()'s own
    # comment on the live bug this fixed (UI showed a "Buy below" ceiling
    # a card's real listing price never supported, alongside a profit
    # figure secretly computed from a cheaper, never-displayed number).
    def test_expected_profit_matches_trading_math_net_profit(self):
        ci = evaluate_card(_snapshot(), as_of=AS_OF)
        assert ci.recommended_sell_target is not None
        assert ci.recommended_buy_max is not None
        expected = tm.net_profit(ci.recommended_sell_target, ci.recommended_buy_max)
        assert ci.expected_profit_after_tax == expected

    def test_expected_roi_matches_trading_math_net_roi(self):
        ci = evaluate_card(_snapshot(), as_of=AS_OF)
        expected = tm.net_roi(ci.recommended_sell_target, ci.recommended_buy_max)
        assert ci.expected_roi == expected

    def test_sell_target_is_a_valid_ea_increment(self):
        ci = evaluate_card(_snapshot(), as_of=AS_OF)
        assert ci.recommended_sell_target == tm.round_to_ea_increment(ci.recommended_sell_target)


class TestBuyMaxNeverExceedsLivePrice:
    """Regression test for a live bug: on a card whose sales evidence
    pushed fair_value well above the live BIN, the "Buy below" ceiling
    (derived purely from fair_value) came out higher than the actual
    current listing price - a real card showed "Buy below 205,000"
    while current_bin was ~176,000, alongside an "Expected profit"
    silently computed against the never-displayed 176,000. Advising a
    buy ceiling above what the card is actually listed for is bad advice
    on its own, and the two displayed numbers didn't reconcile."""

    def test_buy_max_clamped_to_current_bin_when_formula_ceiling_is_higher(self):
        # Sales evidence far above current_bin pushes the raw formula
        # ceiling (~118,000) above current_bin (100,000).
        ci = evaluate_card(
            _snapshot(
                current_bin=100000, sales_median=140000, sales_trimmed_mean=140000,
                sales_low=130000, sales_high=150000, sales_stddev=4000,
                sales_dispersion_ratio=0.03,
            ),
            as_of=AS_OF,
        )
        assert ci.recommended_buy_max is not None
        assert ci.recommended_buy_max <= 100000
        # The displayed profit must reconcile with the displayed buy/sell
        # numbers - simple arithmetic a user can verify themselves.
        expected_profit = tm.net_profit(ci.recommended_sell_target, ci.recommended_buy_max)
        assert ci.expected_profit_after_tax == expected_profit

    def test_buy_max_uses_formula_ceiling_when_it_is_the_lower_bound(self):
        # Default fixture: formula ceiling (9800) is already below
        # current_bin (10000) - clamping must be a true no-op here.
        ci = evaluate_card(_snapshot(), as_of=AS_OF)
        assert ci.recommended_buy_max == 9800


class TestSignalDecision:
    def test_strong_discount_with_good_confidence_yields_buy_or_strong_buy(self):
        # BIN well below sales evidence, tight sample, fresh price.
        ci = evaluate_card(
            _snapshot(current_bin=9000, sales_median=11500, sales_trimmed_mean=11500, sales_count=40),
            as_of=AS_OF,
        )
        assert ci.signal in ("buy", "strong_buy")
        assert ci.expected_roi is not None and ci.expected_roi > 0
        assert ci.signal_reasons  # concrete, non-empty reasons

    def test_bin_above_fair_value_does_not_signal_buy(self):
        ci = evaluate_card(
            _snapshot(current_bin=15000, sales_median=11000, sales_trimmed_mean=11000),
            as_of=AS_OF,
        )
        assert ci.signal in ("avoid", "hold", "watch")

    def test_signal_reasons_are_concrete_and_value_grounded(self):
        ci = evaluate_card(_snapshot(current_bin=9500), as_of=AS_OF)
        joined = " ".join(ci.signal_reasons)
        # At least one reason should reference the actual sample size or
        # a concrete number, not just a generic label.
        assert any(char.isdigit() for char in joined)


class TestDecimalFromAsyncpg:
    """Regression test for a live 500: asyncpg returns Postgres numeric
    columns as Decimal, not float. futgg_market_snapshot's
    sales_window_span_minutes/sales_dispersion_ratio columns hit this in
    production (`TypeError: unsupported operand type(s) for /: 'decimal.Decimal'
    and 'float'` inside _compute_liquidity_score's `span_minutes / 60.0`) even
    though every test fixture here had only ever used plain floats/ints,
    which is exactly why it wasn't caught earlier."""

    def test_decimal_span_minutes_does_not_raise(self):
        ci = evaluate_card(
            _snapshot(sales_window_span_minutes=Decimal("853.0"), sales_count=40),
            as_of=AS_OF,
        )
        assert ci.liquidity_score is not None

    def test_decimal_span_minutes_over_an_hour_formats_in_reasons(self):
        # Exercises the second Decimal/float division site (the
        # "occurred over the last N hours" reason string), which a
        # short-span fixture never reaches.
        ci = evaluate_card(
            _snapshot(sales_window_span_minutes=Decimal("125.0"), sales_count=40),
            as_of=AS_OF,
        )
        assert any("hour" in reason for reason in ci.signal_reasons)

    def test_decimal_dispersion_ratio_does_not_raise(self):
        ci = evaluate_card(
            _snapshot(sales_dispersion_ratio=Decimal("0.0180")),
            as_of=AS_OF,
        )
        assert ci.confidence_score is not None


class TestApproximateTimeNeverClaimedExact:
    def test_intelligence_output_has_no_exact_timestamp_claim(self):
        ci = evaluate_card(_snapshot(), as_of=AS_OF)
        # price_age_minutes is derived from bin_captured_at (a real DB
        # timestamp, not a sales approximation) - sanity check it's an int.
        assert isinstance(ci.price_age_minutes, int)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
