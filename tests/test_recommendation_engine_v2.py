"""
Tests for app/services/recommendation_engine_v2.py's pure decision layer
(evaluate() and its helpers) - no database, no I/O. All fixtures below
were built by actually running evaluate() against candidate inputs and
verifying the resulting numbers by hand (see the module's own dev
history), not guessed and then adjusted to make assertions pass.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.services.recommendation_engine_v2 import (
    EvaluationInputs,
    MarketSnapshot,
    evaluate,
    run_hard_gates,
)

AS_OF = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)


def _bin_series(prices, hours_apart=4, start=None):
    start = start or AS_OF - timedelta(hours=48)
    return [(start + timedelta(hours=i * hours_apart), p) for i, p in enumerate(prices)]


def _base_market(**overrides):
    defaults = dict(
        card_id=1, platform="ps",
        entry_price=10000, price_captured_at=AS_OF - timedelta(minutes=1),
        fair_value_24h=11000, fair_value_7d=10800,
        sales_24h=20, sales_7d=140, sales_per_hour_24h=Decimal("0.83"),
        volatility_24h=300, bin_zscore_24h=Decimal("-2.0"),
        trend_falling=False, data_quality_suspect=False,
    )
    defaults.update(overrides)
    return MarketSnapshot(**defaults)


# A gently-rising bin history and a sales sample clustered around
# 10500-11450 - verified to produce status=BUY with quick_flip AND
# low_risk both qualifying (multiple strategies, independently).
GOOD_SALES = [10500 + i * 50 for i in range(20)]
GOOD_BIN_OBS = _bin_series(
    [9950, 9970, 9960, 9980, 9975, 9990, 9985, 10000, 9995, 10005, 10000, 10010, 10000]
)


class TestHardGates:
    def test_valid_inputs_pass_all_gates(self):
        inputs = EvaluationInputs(market=_base_market(), sales_24h_prices=GOOD_SALES, sales_7d_prices=GOOD_SALES, bin_observations=GOOD_BIN_OBS)
        assert run_hard_gates(inputs, AS_OF) == []

    def test_missing_price_reason_code(self):
        inputs = EvaluationInputs(market=_base_market(entry_price=None), sales_24h_prices=GOOD_SALES, sales_7d_prices=GOOD_SALES, bin_observations=GOOD_BIN_OBS)
        assert "MISSING_PRICE" in run_hard_gates(inputs, AS_OF)

    def test_suspect_data_reason_code(self):
        inputs = EvaluationInputs(market=_base_market(data_quality_suspect=True), sales_24h_prices=GOOD_SALES, sales_7d_prices=GOOD_SALES, bin_observations=GOOD_BIN_OBS)
        assert "SUSPECT_DATA" in run_hard_gates(inputs, AS_OF)

    def test_stale_price_reason_code(self):
        inputs = EvaluationInputs(
            market=_base_market(price_captured_at=AS_OF - timedelta(hours=3)),
            sales_24h_prices=GOOD_SALES, sales_7d_prices=GOOD_SALES, bin_observations=GOOD_BIN_OBS,
        )
        assert "STALE_PRICE" in run_hard_gates(inputs, AS_OF)

    def test_missing_fair_value_reason_code(self):
        inputs = EvaluationInputs(market=_base_market(fair_value_24h=None), sales_24h_prices=GOOD_SALES, sales_7d_prices=GOOD_SALES, bin_observations=GOOD_BIN_OBS)
        assert "MISSING_FAIR_VALUE" in run_hard_gates(inputs, AS_OF)

    def test_insufficient_completed_sales_reason_code(self):
        inputs = EvaluationInputs(market=_base_market(), sales_24h_prices=[10000, 10100], sales_7d_prices=[10000, 10100], bin_observations=GOOD_BIN_OBS)
        assert "INSUFFICIENT_COMPLETED_SALES" in run_hard_gates(inputs, AS_OF)

    def test_insufficient_bin_history_reason_code(self):
        inputs = EvaluationInputs(market=_base_market(), sales_24h_prices=GOOD_SALES, sales_7d_prices=GOOD_SALES, bin_observations=[])
        assert "INSUFFICIENT_BIN_HISTORY" in run_hard_gates(inputs, AS_OF)

    def test_missing_held_cost_basis_reason_code(self):
        inputs = EvaluationInputs(
            market=_base_market(), sales_24h_prices=GOOD_SALES, sales_7d_prices=GOOD_SALES,
            bin_observations=GOOD_BIN_OBS, is_held=True, held_purchase_price=None,
        )
        assert "MISSING_HELD_COST_BASIS" in run_hard_gates(inputs, AS_OF)

    def test_returns_all_failed_reasons_not_just_first(self):
        inputs = EvaluationInputs(
            market=_base_market(entry_price=None, data_quality_suspect=True, fair_value_24h=None),
            sales_24h_prices=[], sales_7d_prices=[], bin_observations=[],
        )
        reasons = run_hard_gates(inputs, AS_OF)
        assert "MISSING_PRICE" in reasons
        assert "SUSPECT_DATA" in reasons
        assert "MISSING_FAIR_VALUE" in reasons
        assert "INSUFFICIENT_COMPLETED_SALES" in reasons
        assert "INSUFFICIENT_BIN_HISTORY" in reasons
        assert len(reasons) > 1


class TestDecisions:
    def test_buy_with_multiple_qualifying_strategies(self):
        inputs = EvaluationInputs(market=_base_market(), sales_24h_prices=GOOD_SALES, sales_7d_prices=GOOD_SALES, bin_observations=GOOD_BIN_OBS)
        r = evaluate(inputs, AS_OF)
        assert r.status == "BUY"
        assert "quick_flip" in r.qualified_strategies
        assert "low_risk" in r.qualified_strategies
        # No global ranking - both independently qualified, not one
        # "winner" selected over the other.
        assert len(r.qualified_strategies) >= 2

    def test_avoid_when_likely_roi_negative(self):
        market = _base_market(entry_price=11000, fair_value_24h=10000, fair_value_7d=10000, bin_zscore_24h=Decimal("1.0"))
        sales = [9500 + i * 50 for i in range(20)]
        bin_obs = _bin_series([11000] * 13)
        inputs = EvaluationInputs(market=market, sales_24h_prices=sales, sales_7d_prices=sales, bin_observations=bin_obs)
        r = evaluate(inputs, AS_OF)
        assert r.status == "AVOID"
        assert r.likely_net_roi is not None and r.likely_net_roi < 0
        assert r.qualified_strategies == []

    def test_wait_when_valid_but_no_strategy_qualifies(self):
        market = _base_market(entry_price=10000, fair_value_24h=10700, fair_value_7d=10650, bin_zscore_24h=Decimal("-0.5"))
        sales = [10584 + i * 10 for i in range(20)]
        bin_obs = _bin_series([9990, 10000, 9995, 10005, 10000, 9998, 10002, 10000, 9997, 10003, 10000, 10001, 10000])
        inputs = EvaluationInputs(market=market, sales_24h_prices=sales, sales_7d_prices=sales, bin_observations=bin_obs)
        r = evaluate(inputs, AS_OF)
        assert r.status == "WAIT"
        assert r.likely_net_roi is not None and r.likely_net_roi > 0
        assert r.qualified_strategies == []
        assert r.failed_gate_reasons == []

    def test_insufficient_data_short_circuits_before_any_strategy_evaluation(self):
        inputs = EvaluationInputs(market=_base_market(data_quality_suspect=True), sales_24h_prices=GOOD_SALES, sales_7d_prices=GOOD_SALES, bin_observations=GOOD_BIN_OBS)
        r = evaluate(inputs, AS_OF)
        assert r.status == "INSUFFICIENT_DATA"
        assert r.strategy_results == {}
        assert r.qualified_strategies == []
        assert "SUSPECT_DATA" in r.failed_gate_reasons

    def test_sbc_never_qualifies_without_real_sbc_data(self):
        inputs = EvaluationInputs(market=_base_market(), sales_24h_prices=GOOD_SALES, sales_7d_prices=GOOD_SALES, bin_observations=GOOD_BIN_OBS, sbc_relevant=None)
        r = evaluate(inputs, AS_OF)
        assert r.strategy_results["sbc"].qualified is False
        assert r.strategy_results["sbc"].reasons == ["NO_SBC_DATA"]
        assert "sbc" not in r.qualified_strategies

    def test_sbc_qualifies_when_explicitly_marked_relevant(self):
        inputs = EvaluationInputs(market=_base_market(), sales_24h_prices=GOOD_SALES, sales_7d_prices=GOOD_SALES, bin_observations=GOOD_BIN_OBS, sbc_relevant=True)
        r = evaluate(inputs, AS_OF)
        assert r.strategy_results["sbc"].qualified is True
        assert "sbc" in r.qualified_strategies

    def test_status_never_buy_for_the_original_tax_loss_scenario(self):
        # The 19,500 -> 20,000 case again, but end-to-end through the
        # engine rather than just trading_math directly: a market where
        # everything actually clears at ~20,000 against a 19,500 entry
        # must never come out BUY, because likely_net_roi is negative.
        market = _base_market(entry_price=19500, fair_value_24h=20000, fair_value_7d=20000, bin_zscore_24h=Decimal("0.5"))
        sales = [19900 + i * 10 for i in range(20)]  # median ~20000
        bin_obs = _bin_series([19500] * 13)
        inputs = EvaluationInputs(market=market, sales_24h_prices=sales, sales_7d_prices=sales, bin_observations=bin_obs)
        r = evaluate(inputs, AS_OF)
        assert r.status != "BUY"
        assert r.likely_net_roi is not None and r.likely_net_roi < 0


class TestHeldPosition:
    def test_missing_purchase_price_is_insufficient_data(self):
        inputs = EvaluationInputs(
            market=_base_market(), sales_24h_prices=GOOD_SALES, sales_7d_prices=GOOD_SALES,
            bin_observations=GOOD_BIN_OBS, is_held=True, held_purchase_price=None,
        )
        r = evaluate(inputs, AS_OF)
        assert r.status == "INSUFFICIENT_DATA"
        assert "MISSING_HELD_COST_BASIS" in r.failed_gate_reasons

    def test_held_fields_use_purchase_price_not_current_bin(self):
        # entry_price (current BIN) is 10000, but the user actually paid
        # 8000 - current_exit_net_roi must be computed against 8000, not
        # silently substituted with entry_price.
        inputs = EvaluationInputs(
            market=_base_market(), sales_24h_prices=GOOD_SALES, sales_7d_prices=GOOD_SALES,
            bin_observations=GOOD_BIN_OBS, is_held=True, held_purchase_price=8000,
        )
        r = evaluate(inputs, AS_OF)
        assert r.purchase_price == 8000
        # net_profit(10000, 8000) = 10000*0.95 - 8000 = 1500
        assert r.current_exit_net_profit == Decimal("1500.00")
        assert r.current_exit_net_roi == Decimal("1500.00") / Decimal("8000")

    def test_held_decision_is_sell_when_profitable_and_momentum_negative(self):
        falling_bins = _bin_series([10500, 10450, 10400, 10350, 10300, 10250, 10200, 10150, 10100, 10050, 10000, 9950, 9900])
        market = _base_market(entry_price=10000, trend_falling=True)
        inputs = EvaluationInputs(
            market=market, sales_24h_prices=GOOD_SALES, sales_7d_prices=GOOD_SALES,
            bin_observations=falling_bins, is_held=True, held_purchase_price=7000,
        )
        r = evaluate(inputs, AS_OF)
        assert r.current_exit_net_profit is not None and r.current_exit_net_profit > 0  # profitable now
        assert r.score_momentum is not None and r.score_momentum < 0  # trend_falling forces this
        assert r.held_decision == "SELL"

    def test_held_decision_is_hold_when_outlook_still_favours_holding(self):
        # Bought at 9500, currently break-even at exit (current_bin
        # 10000), but the likely future case is meaningfully better
        # (likely_hold_net_roi ~0.0975) and momentum/risk are unremarkable
        # (GOOD_BIN_OBS is gently rising, not falling) - HOLD, not SELL.
        inputs = EvaluationInputs(
            market=_base_market(), sales_24h_prices=GOOD_SALES, sales_7d_prices=GOOD_SALES,
            bin_observations=GOOD_BIN_OBS, is_held=True, held_purchase_price=9500,
        )
        r = evaluate(inputs, AS_OF)
        assert r.held_decision == "HOLD"
        assert r.current_exit_net_roi == Decimal("0.00")
        assert r.incremental_hold_value is not None and r.incremental_hold_value > 0
