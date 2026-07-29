from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.services import trading_math as tm
from app.services.ml_label_filler import compute_label_outcome

WINDOW_START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _at(hours: float) -> datetime:
    return WINDOW_START + timedelta(hours=hours)


class TestTargetReached:
    def test_target_reached_on_first_qualifying_sale(self):
        sales = [(9800, _at(1)), (10500, _at(5)), (10200, _at(10))]
        outcome = compute_label_outcome(sales, entry_price=10000, target_price=10500, window_start=WINDOW_START)
        assert outcome.target_reached is True
        assert outcome.realized_sale_price == 10500
        assert outcome.realized_at == _at(5)
        assert outcome.time_to_target_minutes == 5 * 60
        assert outcome.strategy_realized_return == tm.net_roi(10500, 10000)

    def test_target_not_reached_falls_back_to_mark_to_market(self):
        sales = [(9800, _at(1)), (10100, _at(20))]
        outcome = compute_label_outcome(sales, entry_price=10000, target_price=10500, window_start=WINDOW_START)
        assert outcome.target_reached is False
        assert outcome.realized_sale_price is None
        assert outcome.mark_to_market_price == 10100
        assert outcome.strategy_realized_return == tm.net_roi(10100, 10000)


class TestNoActivity:
    def test_no_sales_falls_back_to_last_known_bin(self):
        outcome = compute_label_outcome([], entry_price=10000, target_price=10500, window_start=WINDOW_START, last_known_bin=9700)
        assert outcome.mark_to_market_price == 9700
        assert outcome.mark_to_market_return == tm.net_roi(9700, 10000)
        assert outcome.no_market_activity_in_window is False  # a real BIN observation is activity, just not a sale

    def test_no_sales_and_no_bin_is_genuinely_no_activity(self):
        outcome = compute_label_outcome([], entry_price=10000, target_price=10500, window_start=WINDOW_START, last_known_bin=None)
        assert outcome.mark_to_market_price is None
        assert outcome.mark_to_market_return is None
        assert outcome.strategy_realized_return is None
        assert outcome.no_market_activity_in_window is True


class TestExcursions:
    def test_max_favourable_and_adverse_excursion(self):
        sales = [(10000, _at(1)), (11000, _at(3)), (9000, _at(6)), (10200, _at(10))]
        outcome = compute_label_outcome(sales, entry_price=10000, target_price=99999, window_start=WINDOW_START)
        assert outcome.max_favourable_excursion == tm.net_roi(11000, 10000)
        assert outcome.max_adverse_excursion == tm.net_roi(9000, 10000)

    def test_no_entry_price_means_no_returns_computed(self):
        sales = [(10000, _at(1))]
        outcome = compute_label_outcome(sales, entry_price=None, target_price=10500, window_start=WINDOW_START)
        assert outcome.mark_to_market_return is None
        assert outcome.strategy_realized_return is None
        assert outcome.max_favourable_excursion is None
        assert outcome.max_adverse_excursion is None

    def test_no_target_price_never_marks_target_reached(self):
        sales = [(50000, _at(1))]
        outcome = compute_label_outcome(sales, entry_price=10000, target_price=None, window_start=WINDOW_START)
        assert outcome.target_reached is False
        assert outcome.realized_sale_price is None
        # still marks-to-market off the real sale even with no target to grade against
        assert outcome.mark_to_market_price == 50000
