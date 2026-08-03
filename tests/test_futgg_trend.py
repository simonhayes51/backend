# tests/test_futgg_trend.py
#
# Pure unit tests for the trend / falling-knife layer. No DB, no I/O -
# evaluate_trend() takes observations and returns a dataclass.
#
# The scenarios here are the ones the engine previously got wrong: a card
# in a steep unresolved decline used to read as the *best* opportunity
# available (bigger fall => bigger discount to a stale median => higher
# apparent ROI), because there was no trend term at all.
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.futgg_trend import (
    DOWNTREND, FALLING_KNIFE, INSUFFICIENT_TREND_DATA, RECOVERING,
    SIDEWAYS, STABILISING, UPTREND, evaluate_trend,
)

NOW = datetime(2026, 8, 3, 18, 0, tzinfo=timezone.utc)


def _series(prices, *, start_hours_ago=48.0, end_hours_ago=0.0):
    """Evenly-timed series helper. Spacing is computed from real
    timestamps, so callers can also pass explicit gaps where a test cares
    about uneven spacing."""
    n = len(prices)
    if n == 1:
        return [(prices[0], NOW - timedelta(hours=start_hours_ago))]
    step = (start_hours_ago - end_hours_ago) / (n - 1)
    return [
        (price, NOW - timedelta(hours=start_hours_ago - step * i))
        for i, price in enumerate(prices)
    ]


class TestInsufficientData:
    def test_too_few_observations(self):
        result = evaluate_trend(_series([100_000, 99_000, 98_000]), as_of=NOW)
        assert result.state == INSUFFICIENT_TREND_DATA
        assert result.blocks_value_read is False

    def test_enough_observations_but_time_compressed(self):
        # Twelve sales inside four minutes says nothing about a trend.
        sales = [
            (100_000 - i * 500, NOW - timedelta(seconds=240 - i * 20))
            for i in range(12)
        ]
        result = evaluate_trend(sales, as_of=NOW)
        assert result.state == INSUFFICIENT_TREND_DATA

    def test_empty_input(self):
        assert evaluate_trend([], as_of=NOW).state == INSUFFICIENT_TREND_DATA


class TestFallingKnife:
    def test_steep_unresolved_decline_is_a_knife(self):
        # ~35% decline, still accelerating down at the end. This is the
        # exact shape that used to produce the engine's strongest buy.
        prices = [
            200_000, 197_000, 193_000, 188_000, 182_000, 175_000,
            166_000, 156_000, 145_000, 136_000, 129_000, 124_000,
        ]
        result = evaluate_trend(_series(prices), as_of=NOW)
        assert result.state == FALLING_KNIFE
        assert result.blocks_value_read is True
        assert result.features.drawdown_from_high > 0.30
        assert result.features.below_prior_median_ratio == 1.0

    def test_knife_description_is_plain_english(self):
        prices = [200_000, 196_000, 190_000, 182_000, 172_000, 160_000,
                  147_000, 135_000, 126_000, 120_000]
        result = evaluate_trend(_series(prices), as_of=NOW)
        assert result.state == FALLING_KNIFE
        assert "still" in result.description.lower()
        assert "falling" in result.description.lower()


class TestStabilising:
    def test_fell_then_flattened(self):
        # Big early drop, then a genuinely flat tail that makes no new lows.
        prices = [
            200_000, 190_000, 178_000, 165_000, 158_000, 154_000,
            153_000, 154_000, 153_500, 154_000, 153_000, 154_500,
        ]
        result = evaluate_trend(_series(prices), as_of=NOW)
        assert result.state == STABILISING
        # Stabilising is NOT treated as an unresolved bearish state - the
        # discount has stopped widening, so a value read is permitted
        # (the engine caps it at 'buy' rather than blocking it).
        assert result.blocks_value_read is False
        assert result.features.new_low_ratio == 0.0


class TestRecovering:
    def test_dropped_then_turned_up_off_the_low(self):
        prices = [
            200_000, 188_000, 174_000, 160_000, 150_000, 144_000,
            142_000, 148_000, 155_000, 161_000, 166_000, 170_000,
        ]
        result = evaluate_trend(_series(prices), as_of=NOW)
        assert result.state == RECOVERING
        assert result.blocks_value_read is False
        assert result.features.bounce_from_low > 0.05


class TestSidewaysAndUptrend:
    def test_flat_liquid_market_is_sideways(self):
        prices = [
            100_000, 101_000, 99_500, 100_500, 99_000, 100_000,
            101_000, 100_000, 99_500, 100_500, 100_000, 99_800,
        ]
        result = evaluate_trend(_series(prices), as_of=NOW)
        assert result.state == SIDEWAYS
        assert result.blocks_value_read is False

    def test_steady_climb_is_uptrend(self):
        prices = [
            100_000, 103_000, 106_000, 109_000, 112_000, 116_000,
            120_000, 124_000, 128_000, 132_000, 136_000, 140_000,
        ]
        result = evaluate_trend(_series(prices), as_of=NOW)
        assert result.state == UPTREND
        assert result.features.medium_term_change > 0


class TestDowntrend:
    def test_steady_drift_down_is_downtrend_not_knife(self):
        # A real but orderly decline - no steep recent leg, so it must not
        # be classified as a knife.
        prices = [
            100_000, 99_000, 98_000, 97_500, 96_500, 95_500,
            95_000, 94_000, 93_500, 92_500, 92_000, 91_000,
        ]
        result = evaluate_trend(_series(prices), as_of=NOW)
        assert result.state == DOWNTREND
        assert result.blocks_value_read is True


class TestUnevenSpacing:
    def test_time_split_not_index_split(self):
        # 20 sales crammed into the last 30 minutes, 6 spread over the
        # previous 3 days. An index-based split would call the last 40% of
        # ROWS "recent" - all inside half an hour - and compare it against
        # rows that are also recent, concluding "flat". Splitting by TIME
        # correctly compares the last stretch against the older stretch.
        old = [
            (200_000, NOW - timedelta(hours=72)),
            (198_000, NOW - timedelta(hours=60)),
            (199_000, NOW - timedelta(hours=48)),
            (197_000, NOW - timedelta(hours=36)),
            (198_500, NOW - timedelta(hours=24)),
            (197_500, NOW - timedelta(hours=12)),
        ]
        burst = [
            (150_000 - i * 200, NOW - timedelta(minutes=30 - i))
            for i in range(20)
        ]
        result = evaluate_trend(old + burst, as_of=NOW)
        # The level genuinely moved from ~198k to ~150k; must be bearish.
        assert result.state in (FALLING_KNIFE, DOWNTREND)
        assert result.features.medium_term_change < -0.15

    def test_all_identical_timestamps_does_not_crash(self):
        same = [(100_000 + i * 100, NOW - timedelta(hours=5)) for i in range(10)]
        result = evaluate_trend(same, as_of=NOW)
        # Zero span - cannot be a trend.
        assert result.state == INSUFFICIENT_TREND_DATA


class TestInputShapes:
    def test_accepts_provider_dict_rows(self):
        rows = [
            {"sold_price": p, "approximate_sold_at": t}
            for p, t in _series([100_000, 99_000, 98_000, 97_000,
                                 96_000, 95_000, 94_000, 93_000])
        ]
        result = evaluate_trend(rows, as_of=NOW)
        assert result.state != INSUFFICIENT_TREND_DATA
        assert result.features.observation_count == 8

    def test_skips_unusable_rows_without_crashing(self):
        rows = [
            {"sold_price": None, "approximate_sold_at": NOW},
            {"sold_price": 0, "approximate_sold_at": NOW},
            {"sold_price": "not-a-number", "approximate_sold_at": NOW},
        ] + [
            {"sold_price": p, "approximate_sold_at": t}
            for p, t in _series([100_000, 99_000, 98_000, 97_000, 96_000, 95_000])
        ]
        result = evaluate_trend(rows, as_of=NOW)
        assert result.features.observation_count == 6

    def test_naive_datetimes_are_treated_as_utc(self):
        naive = [
            (p, t.replace(tzinfo=None))
            for p, t in _series([100_000, 99_000, 98_000, 97_000, 96_000, 95_000])
        ]
        result = evaluate_trend(naive, as_of=NOW)
        assert result.state != INSUFFICIENT_TREND_DATA


class TestVersioning:
    def test_assessment_carries_version_and_features(self):
        result = evaluate_trend(_series([100_000] * 8), as_of=NOW)
        assert result.version.startswith("trend-")
        payload = result.as_dict()
        assert "features" in payload and "state" in payload
        assert payload["features"]["observation_count"] == 8
