"""
Tests for app/services/futgg_intelligence.py's pure evaluate_card() layer
- no database, no I/O. Snapshot dicts below mimic rows of the
futgg_market_snapshot materialized view (migrations/038).
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.services import trading_math as tm
from app.services import futgg_reasons as reasons_pkg
from app.services.futgg_intelligence import (
    DEFAULT_MAX_ACCEPTABLE_PRICE_AGE_MINUTES,
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
        stale_at = AS_OF - timedelta(minutes=DEFAULT_MAX_ACCEPTABLE_PRICE_AGE_MINUTES + 1)
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
            _snapshot(bin_captured_at=AS_OF - timedelta(minutes=DEFAULT_MAX_ACCEPTABLE_PRICE_AGE_MINUTES - 5)),
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


class TestSeparatedBuyPrices:
    """Engine v2, item 10: the four prices are distinct, and none of them
    is produced by substituting the card's own asking price into the
    calculation.

    The previous behaviour clamped recommended_buy_max to current_bin,
    which made the advice circular - "the most you should pay" became
    "whatever it costs", so any card could be presented as a valid buy at
    its own ask. These tests pin the corrected contract."""

    def test_all_four_prices_are_present_and_ordered(self):
        ci = evaluate_card(
            _snapshot(
                current_bin=100000, sales_median=140000, sales_trimmed_mean=140000,
                sales_low=130000, sales_high=150000, sales_stddev=4000,
                sales_dispersion_ratio=0.03,
            ),
            as_of=AS_OF,
        )
        # The theoretical ceiling is derived from fair value alone and is
        # deliberately allowed to sit ABOVE the live ask - that is the
        # point: it describes how much headroom the card has, it is not a
        # price we advise paying.
        assert ci.theoretical_max_buy > 100000
        # The advised ceiling is strictly below the theoretical one.
        assert ci.recommended_buy_max < ci.theoretical_max_buy
        # The live ask is below the advised ceiling here, so it is buyable.
        assert ci.current_executable_buy == 100000
        assert ci.break_even_price is not None
        assert ci.recommended_sell_target > ci.break_even_price

    def test_profit_reconciles_with_the_price_a_user_would_actually_pay(self):
        ci = evaluate_card(
            _snapshot(
                current_bin=100000, sales_median=140000, sales_trimmed_mean=140000,
                sales_low=130000, sales_high=150000, sales_stddev=4000,
                sales_dispersion_ratio=0.03,
            ),
            as_of=AS_OF,
        )
        # Arithmetic a user can verify by hand from the displayed numbers.
        assert ci.expected_profit_after_tax == tm.net_profit(
            ci.recommended_sell_target, ci.current_executable_buy
        )

    def test_bin_above_recommended_max_is_a_watch_not_a_buy(self):
        # Default fixture: theoretical ceiling 9,800, advised ceiling
        # 9,400, live BIN 10,000. Previously the ceiling was clamped down
        # to 10,000 and this presented as a buy. It is not one.
        ci = evaluate_card(_snapshot(), as_of=AS_OF)
        assert ci.theoretical_max_buy == 9800
        assert ci.recommended_buy_max == 9400
        assert ci.current_executable_buy is None
        assert ci.signal == "watch"
        assert ci.status == "watch"
        assert ci.buy_below == 9400
        assert reasons_pkg.PRICE_ABOVE_MAX_BUY in ci.reason_codes

    def test_watch_states_an_explicit_trigger_price_in_english(self):
        ci = evaluate_card(_snapshot(), as_of=AS_OF)
        joined = " ".join(ci.signal_reasons)
        assert "9,400" in joined
        assert "falls to" in joined


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


class TestTierAwareStaleness:
    """A flat 120-minute freshness cutoff applied to every card equally
    was itself a product bug: a "buy now" tip on a fast-moving special
    card could sit confidently presented as fresh right up to the edge
    of its own hour-long refresh interval, by which point the discount
    that made it a tip may already be gone. Each price_tier now has its
    own, much tighter, threshold."""

    def test_special_tier_flags_stale_well_before_default_threshold(self):
        # 30 minutes old: well within the old flat 120-minute cutoff, but
        # beyond special's own 20-minute threshold.
        ci = evaluate_card(
            _snapshot(price_tier="special", bin_captured_at=AS_OF - timedelta(minutes=30)),
            as_of=AS_OF,
        )
        assert ci.signal == "insufficient_data"
        assert any("special-tier" in r for r in ci.signal_reasons)

    def test_bronze_tier_tolerates_an_age_that_would_flag_special(self):
        # Same 30-minute age a special-tier card was flagged stale at -
        # bronze's much longer market cadence tolerates this fine.
        ci = evaluate_card(
            _snapshot(price_tier="bronze", bin_captured_at=AS_OF - timedelta(minutes=30)),
            as_of=AS_OF,
        )
        assert ci.signal != "insufficient_data"

    def test_unknown_tier_falls_back_to_default_threshold(self):
        stale_at = AS_OF - timedelta(minutes=DEFAULT_MAX_ACCEPTABLE_PRICE_AGE_MINUTES - 5)
        ci = evaluate_card(
            _snapshot(price_tier=None, bin_captured_at=stale_at),
            as_of=AS_OF,
        )
        assert ci.signal != "insufficient_data"
        too_stale = AS_OF - timedelta(minutes=DEFAULT_MAX_ACCEPTABLE_PRICE_AGE_MINUTES + 5)
        ci2 = evaluate_card(
            _snapshot(price_tier=None, bin_captured_at=too_stale),
            as_of=AS_OF,
        )
        assert ci2.signal == "insufficient_data"

    def test_special_tier_confidence_degrades_faster_than_bronze_at_same_age(self):
        age = AS_OF - timedelta(minutes=15)
        special = evaluate_card(_snapshot(price_tier="special", bin_captured_at=age), as_of=AS_OF)
        bronze = evaluate_card(_snapshot(price_tier="bronze", bin_captured_at=age), as_of=AS_OF)
        assert special.confidence_score < bronze.confidence_score


class TestApproximateTimeNeverClaimedExact:
    def test_intelligence_output_has_no_exact_timestamp_claim(self):
        ci = evaluate_card(_snapshot(), as_of=AS_OF)
        # price_age_minutes is derived from bin_captured_at (a real DB
        # timestamp, not a sales approximation) - sanity check it's an int.
        assert isinstance(ci.price_age_minutes, int)


def _falling_series(start_price, end_price, *, n=12, hours=48.0):
    """Monotonic decline with a steepening tail - the shape that used to
    produce the engine's strongest buy signals."""
    step = hours / (n - 1)
    out = []
    for i in range(n):
        # Quadratic easing so the decline accelerates rather than being linear.
        frac = (i / (n - 1)) ** 1.6
        price = start_price + (end_price - start_price) * frac
        out.append((price, AS_OF - timedelta(hours=hours - step * i)))
    return out


def _flat_series(price, *, n=12, hours=48.0):
    step = hours / (n - 1)
    return [
        (price + (50 if i % 2 else -50), AS_OF - timedelta(hours=hours - step * i))
        for i in range(n)
    ]


class TestFallingKnifeGate:
    """The single most important behavioural change in engine v2.

    A card that has fallen hard has a stale-high sales median, so the
    naive "BIN vs median" comparison scores it as the *biggest* discount
    available. With no trend term, the engine ranked exactly the cards
    that were still collapsing as its top opportunities. These tests pin
    the gate that stops that."""

    def test_steep_decline_below_stale_median_is_not_a_buy(self):
        # BIN 60,000 against a 100,000 median: a 40% "discount" on the
        # old logic, and a large positive expected ROI. It is a card in
        # free fall.
        snapshot = _snapshot(
            current_bin=60000, sales_median=100000, sales_trimmed_mean=100000,
            sales_low=58000, sales_high=140000, sales_stddev=12000,
            sales_dispersion_ratio=0.12, sales_count=30,
        )
        ci = evaluate_card(
            snapshot, as_of=AS_OF, sales=_falling_series(140000, 62000),
        )
        assert ci.trend_state in ("falling_knife", "downtrend")
        assert ci.signal == "avoid"
        assert ci.risk_level == "high"
        assert (
            reasons_pkg.FALLING_KNIFE in ci.reason_codes
            or reasons_pkg.UNRESOLVED_DOWNTREND in ci.reason_codes
        )

    def test_same_card_without_the_trend_gate_would_have_looked_great(self):
        # Identical snapshot, no sales series supplied. This documents
        # exactly how large the mispricing looked before the gate existed
        # - and that omitting the series now degrades to a capped signal
        # rather than silently reverting to the old behaviour.
        snapshot = _snapshot(
            current_bin=60000, sales_median=100000, sales_trimmed_mean=100000,
            sales_low=58000, sales_high=140000, sales_stddev=12000,
            sales_dispersion_ratio=0.12, sales_count=30,
        )
        ci = evaluate_card(snapshot, as_of=AS_OF)
        assert ci.expected_roi > 0.30  # the discount the old engine chased
        assert ci.trend_state == "insufficient_trend_data"
        # Capped, never promoted to strong_buy, because the trend is unknown.
        assert ci.signal != "strong_buy"

    def test_stabilised_card_below_median_is_allowed_but_capped(self):
        # Fell, then genuinely flattened. This IS a legitimate buy - the
        # gate must not be so blunt that it blocks every discounted card.
        sales = _falling_series(120000, 92000, n=7, hours=48.0) + _flat_series(
            91500, n=7, hours=16.0
        )
        snapshot = _snapshot(
            current_bin=88000, sales_median=95000, sales_trimmed_mean=95000,
            sales_low=88000, sales_high=120000, sales_stddev=5000,
            sales_dispersion_ratio=0.05, sales_count=30,
        )
        ci = evaluate_card(snapshot, as_of=AS_OF, sales=sales)
        assert ci.trend_state in ("stabilising", "sideways", "recovering")
        assert ci.signal in ("buy", "strong_buy", "watch")
        assert ci.signal != "avoid"

    def test_sideways_liquid_market_still_produces_a_buy(self):
        snapshot = _snapshot(
            current_bin=9000, sales_median=11500, sales_trimmed_mean=11500,
            sales_count=40,
        )
        ci = evaluate_card(snapshot, as_of=AS_OF, sales=_flat_series(11500, n=20))
        assert ci.trend_state == "sideways"
        assert ci.signal in ("buy", "strong_buy")
        assert ci.status == "active"


class TestExpiryAndProvenance:
    def test_every_result_is_versioned(self):
        ci = evaluate_card(_snapshot(), as_of=AS_OF)
        assert ci.engine_version.startswith("futgg-")
        assert ci.trend_version.startswith("trend-")
        assert ci.evaluated_at == AS_OF
        assert ci.evaluated_bin == 10000

    def test_active_recommendation_carries_an_expiry(self):
        ci = evaluate_card(
            _snapshot(current_bin=9000, sales_median=11500, sales_trimmed_mean=11500,
                      sales_count=40),
            as_of=AS_OF, sales=_flat_series(11500, n=20),
        )
        assert ci.expiry_minutes is not None and ci.expiry_minutes > 0
        assert ci.expires_at == AS_OF + timedelta(minutes=ci.expiry_minutes)

    def test_expiry_never_outlives_the_price_freshness_budget(self):
        # A price already 100 minutes old on a 120-minute default tier has
        # ~20 minutes of credibility left, not a fresh full window.
        ci = evaluate_card(
            _snapshot(
                current_bin=9000, sales_median=11500, sales_trimmed_mean=11500,
                sales_count=40, bin_captured_at=AS_OF - timedelta(minutes=100),
            ),
            as_of=AS_OF, sales=_flat_series(11500, n=20),
        )
        assert ci.expiry_minutes <= 20

    def test_stale_price_yields_insufficient_data_with_a_code(self):
        ci = evaluate_card(
            _snapshot(bin_captured_at=AS_OF - timedelta(minutes=600)), as_of=AS_OF,
        )
        assert ci.status == "insufficient_data"
        assert reasons_pkg.STALE_MARKET in ci.reason_codes


class TestStructuredReasons:
    def test_reasons_expose_codes_and_english(self):
        ci = evaluate_card(_snapshot(), as_of=AS_OF)
        assert ci.reasons, "structured reasons must be populated"
        for reason in ci.reasons:
            assert set(reason) == {"code", "message"}
            assert reason["message"]
        # The legacy free-text shape stays available for the existing API.
        assert ci.signal_reasons == [r["message"] for r in ci.reasons]

    def test_blocking_codes_are_separated_from_informational_ones(self):
        ci = evaluate_card(_snapshot(sales_count=1), as_of=AS_OF)
        assert reasons_pkg.INSUFFICIENT_SALES in ci.blocking_codes
        # Informational codes must never appear as blocking.
        assert reasons_pkg.INFO_PRICE_AGE not in ci.blocking_codes


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
