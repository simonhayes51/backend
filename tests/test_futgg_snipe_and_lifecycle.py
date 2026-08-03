# tests/test_futgg_snipe_and_lifecycle.py
#
# Pure tests for the two modules that make a recommendation actionable
# and keep it honest afterwards: the executable snipe filter, and the
# expiry/invalidation lifecycle.
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.futgg_intelligence import (
    STATUS_ACTIVE, STATUS_EXPIRED, STATUS_INVALIDATED, STATUS_WATCH,
    evaluate_card,
)
from app.services.futgg_lifecycle import (
    INVALIDATED_NO_PRICE, INVALIDATED_PRICE_MOVED, INVALIDATED_PRICE_ROSE,
    check_lifecycle,
)
from app.services.futgg_snipe_filter import build_snipe_filter

AS_OF = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)


def _snapshot(**overrides):
    defaults = dict(
        source_card_id=7, name="Erling Haaland", rating=91,
        rarity="gold_rare", primary_position="ST", is_tradeable=True,
        current_bin=9000, bin_captured_at=AS_OF - timedelta(minutes=3),
        sales_count=30, sales_median=11500, sales_trimmed_mean=11500,
        sales_low=11000, sales_high=12000, sales_stddev=200,
        sales_window_span_minutes=240.0, sales_dispersion_ratio=0.02,
    )
    defaults.update(overrides)
    return defaults


def _flat_sales(price=11500, n=20, hours=48.0):
    step = hours / (n - 1)
    return [
        (price + (50 if i % 2 else -50), AS_OF - timedelta(hours=hours - step * i))
        for i in range(n)
    ]


class TestSnipeFilterMaxBin:
    def test_max_bin_is_the_profitable_threshold_not_the_current_ask(self):
        snapshot = _snapshot()
        ci = evaluate_card(snapshot, as_of=AS_OF, sales=_flat_sales())
        sf = build_snipe_filter(snapshot, ci)
        assert sf is not None
        # The whole point: the filter is a trap set at the profitable
        # price, not a mirror of what is already listed.
        assert sf.max_bin == int(ci.recommended_buy_max)
        assert sf.max_bin != snapshot["current_bin"]

    def test_buy_mode_instruction_is_directly_executable(self):
        snapshot = _snapshot()
        ci = evaluate_card(snapshot, as_of=AS_OF, sales=_flat_sales())
        sf = build_snipe_filter(snapshot, ci)
        assert sf.mode == "buy"
        assert "Haaland" in sf.instruction
        assert f"{sf.max_bin:,}" in sf.instruction

    def test_card_above_threshold_still_yields_a_watch_filter(self):
        # A standing search is executable; "too expensive right now" is not.
        snapshot = _snapshot(current_bin=13000)
        ci = evaluate_card(snapshot, as_of=AS_OF, sales=_flat_sales())
        sf = build_snipe_filter(snapshot, ci)
        assert sf is not None
        assert sf.mode == "watch"
        assert ci.signal == "watch"
        assert sf.max_bin == int(ci.recommended_buy_max)

    def test_untradeable_card_yields_no_filter(self):
        snapshot = _snapshot(is_tradeable=False)
        ci = evaluate_card(snapshot, as_of=AS_OF)
        assert build_snipe_filter(snapshot, ci) is None

    def test_filter_carries_execution_context(self):
        snapshot = _snapshot()
        ci = evaluate_card(snapshot, as_of=AS_OF, sales=_flat_sales())
        sf = build_snipe_filter(snapshot, ci)
        assert sf.quality == "Gold"
        assert sf.is_rare is True
        assert sf.position == "ST"
        assert sf.target_sell_price == int(ci.recommended_sell_target)
        assert sf.break_even_price == int(ci.break_even_price)
        assert sf.expires_at == ci.expires_at
        assert sf.recommended_quantity >= 1
        assert sf.expected_hold_label

    def test_unknown_rarity_yields_no_quality_filter_rather_than_a_guess(self):
        # A wrong quality filter silently returns zero results, and the
        # user cannot tell it was our error.
        snapshot = _snapshot(rarity="totw_special")
        ci = evaluate_card(snapshot, as_of=AS_OF, sales=_flat_sales())
        sf = build_snipe_filter(snapshot, ci)
        assert sf.quality is None
        assert sf.is_rare is None

    def test_quantity_stays_conservative_on_illiquid_cards(self):
        snapshot = _snapshot(sales_count=6, sales_window_span_minutes=10000.0)
        ci = evaluate_card(snapshot, as_of=AS_OF, sales=_flat_sales(n=6))
        sf = build_snipe_filter(snapshot, ci)
        if sf is not None:
            assert sf.recommended_quantity == 1


class TestLifecycleInvalidation:
    def test_price_rising_above_threshold_invalidates_an_active_buy(self):
        verdict = check_lifecycle(
            original_status=STATUS_ACTIVE, evaluated_bin=9000,
            recommended_buy_max=9400, expires_at=AS_OF + timedelta(hours=1),
            live_bin=12000, as_of=AS_OF,
        )
        assert verdict.status == STATUS_INVALIDATED
        assert verdict.reason == INVALIDATED_PRICE_ROSE
        assert "12,000" in verdict.message
        assert verdict.is_usable is False

    def test_material_drift_invalidates_even_when_still_below_threshold(self):
        verdict = check_lifecycle(
            original_status=STATUS_ACTIVE, evaluated_bin=9000,
            recommended_buy_max=9400, expires_at=AS_OF + timedelta(hours=1),
            live_bin=8000, as_of=AS_OF,
        )
        assert verdict.status == STATUS_INVALIDATED
        assert verdict.reason == INVALIDATED_PRICE_MOVED

    def test_missing_live_price_invalidates(self):
        verdict = check_lifecycle(
            original_status=STATUS_ACTIVE, evaluated_bin=9000,
            recommended_buy_max=9400, expires_at=AS_OF + timedelta(hours=1),
            live_bin=None, as_of=AS_OF,
        )
        assert verdict.status == STATUS_INVALIDATED
        assert verdict.reason == INVALIDATED_NO_PRICE

    def test_invalidation_is_checked_before_expiry(self):
        # Both old AND wrong. "We know it is wrong" is more informative
        # than "it is old", so invalidation must win.
        verdict = check_lifecycle(
            original_status=STATUS_ACTIVE, evaluated_bin=9000,
            recommended_buy_max=9400, expires_at=AS_OF - timedelta(hours=2),
            live_bin=15000, as_of=AS_OF,
        )
        assert verdict.status == STATUS_INVALIDATED


class TestLifecycleExpiry:
    def test_past_expiry_is_expired(self):
        verdict = check_lifecycle(
            original_status=STATUS_ACTIVE, evaluated_bin=9000,
            recommended_buy_max=9400, expires_at=AS_OF - timedelta(minutes=1),
            live_bin=9050, as_of=AS_OF,
        )
        assert verdict.status == STATUS_EXPIRED
        assert verdict.is_usable is False

    def test_fresh_and_stable_recommendation_survives(self):
        verdict = check_lifecycle(
            original_status=STATUS_ACTIVE, evaluated_bin=9000,
            recommended_buy_max=9400, expires_at=AS_OF + timedelta(minutes=30),
            live_bin=9050, as_of=AS_OF,
        )
        assert verdict.status == STATUS_ACTIVE
        assert verdict.is_usable is True

    def test_watch_above_threshold_is_not_invalidated_for_being_above(self):
        # A watch is BY DEFINITION above its trigger price - that is not
        # a reason to invalidate it, or every watch would die instantly.
        verdict = check_lifecycle(
            original_status=STATUS_WATCH, evaluated_bin=13000,
            recommended_buy_max=9400, expires_at=AS_OF + timedelta(minutes=30),
            live_bin=13100, as_of=AS_OF,
        )
        assert verdict.status == STATUS_WATCH
        assert verdict.is_usable is True

    def test_naive_expiry_timestamps_are_treated_as_utc(self):
        verdict = check_lifecycle(
            original_status=STATUS_ACTIVE, evaluated_bin=9000,
            recommended_buy_max=9400,
            expires_at=(AS_OF + timedelta(minutes=30)).replace(tzinfo=None),
            live_bin=9050, as_of=AS_OF,
        )
        assert verdict.status == STATUS_ACTIVE
