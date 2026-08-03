# tests/test_futgg_outcome_grader.py
#
# Pure tests for chronological outcome grading. The whole value of this
# module rests on one property - that it never uses information or an
# execution that was not available in order - so most of these tests are
# adversarial attempts to make it cheat.
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.futgg_outcome_grader import (
    DOWNSIDE_HIT, FLAT, INSUFFICIENT_OBSERVATIONS, LOSS_UNREALISED,
    NO_ENTRY, PROFITABLE_UNREALISED, TARGET_HIT, grade_recommendation,
)

T0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def _obs(*pairs):
    """(hours_after_T0, price) -> observation tuples."""
    return [(price, T0 + timedelta(hours=h)) for h, price in pairs]


class TestNoHindsight:
    def test_target_hit_before_entry_does_not_count_as_an_exit(self):
        # The price spikes to the target FIRST, then falls to the buy
        # price. A max()-based grader would call this a win. It is not:
        # you could not have sold at a price that printed before you
        # owned the card.
        observations = _obs(
            (1, 120_000),   # target reached - but we have not bought yet
            (2, 88_000),    # NOW we can buy
            (3, 89_000),
            (4, 90_000),
        )
        grade = grade_recommendation(
            horizon="24h", evaluated_at=T0,
            buy_price=90_000, sell_target=115_000,
            observations=observations,
        )
        assert grade.entry_achieved is True
        assert grade.entry_price == 88_000
        assert grade.target_hit is False
        assert grade.outcome_status != TARGET_HIT

    def test_exit_must_be_strictly_after_entry_observation(self):
        observations = _obs((1, 90_000), (2, 130_000))
        grade = grade_recommendation(
            horizon="24h", evaluated_at=T0,
            buy_price=90_000, sell_target=115_000,
            observations=observations,
        )
        assert grade.entry_at == T0 + timedelta(hours=1)
        assert grade.exit_at == T0 + timedelta(hours=2)
        assert grade.exit_at > grade.entry_at

    def test_excursions_measured_only_after_entry(self):
        # A huge dip BEFORE entry must not be counted as our drawdown.
        observations = _obs(
            (1, 40_000),    # crash before we bought - not our loss
            (2, 90_000),    # entry
            (3, 95_000),
            (4, 88_000),
        )
        grade = grade_recommendation(
            horizon="24h", evaluated_at=T0,
            buy_price=90_000, sell_target=200_000,
            observations=observations,
        )
        # Entry is the first observation at or below 90k, which is the
        # 40k crash print - correct, that WAS purchasable.
        assert grade.entry_price == 40_000
        # And the excursions run from there, not from the 90k.
        assert grade.max_adverse_excursion >= 0

    def test_unsorted_input_is_ordered_before_grading(self):
        ordered = _obs((1, 90_000), (2, 130_000))
        shuffled = [ordered[1], ordered[0]]
        a = grade_recommendation(
            horizon="24h", evaluated_at=T0, buy_price=90_000,
            sell_target=115_000, observations=ordered,
        )
        b = grade_recommendation(
            horizon="24h", evaluated_at=T0, buy_price=90_000,
            sell_target=115_000, observations=shuffled,
        )
        assert a.outcome_status == b.outcome_status == TARGET_HIT
        assert a.entry_at == b.entry_at


class TestEntry:
    def test_never_reaching_buy_price_is_no_entry_not_a_loss(self):
        observations = _obs((1, 105_000), (2, 108_000), (3, 112_000))
        grade = grade_recommendation(
            horizon="24h", evaluated_at=T0,
            buy_price=90_000, sell_target=115_000,
            observations=observations,
        )
        assert grade.entry_achieved is False
        assert grade.outcome_status == NO_ENTRY
        # A call that never became a trade contributes no profit figures.
        assert grade.net_profit_after_tax is None
        assert grade.realised_roi is None

    def test_entry_takes_the_first_qualifying_observation_not_the_cheapest(self):
        observations = _obs((1, 89_000), (2, 70_000), (3, 95_000))
        grade = grade_recommendation(
            horizon="24h", evaluated_at=T0,
            buy_price=90_000, sell_target=115_000,
            observations=observations,
        )
        # 70,000 was cheaper, but 89,000 came first and is what a user
        # acting on the recommendation would have paid.
        assert grade.entry_price == 89_000


class TestOutcomeStatuses:
    def test_clean_round_trip_is_target_hit_with_realised_numbers(self):
        observations = _obs((1, 88_000), (5, 120_000), (6, 130_000))
        grade = grade_recommendation(
            horizon="24h", evaluated_at=T0,
            buy_price=90_000, sell_target=115_000,
            observations=observations,
        )
        assert grade.outcome_status == TARGET_HIT
        assert grade.target_hit is True
        assert grade.realised_sell_price == 120_000
        assert grade.minutes_to_target == 4 * 60
        # 120,000 sold after 5% tax = 114,000, minus 88,000 entry.
        assert grade.net_profit_after_tax == 26_000
        assert grade.realised_roi > 0

    def test_up_but_target_never_reached_is_unrealised_not_a_win(self):
        observations = _obs((1, 88_000), (3, 100_000), (5, 104_000))
        grade = grade_recommendation(
            horizon="24h", evaluated_at=T0,
            buy_price=90_000, sell_target=140_000,
            observations=observations,
        )
        assert grade.target_hit is False
        assert grade.outcome_status == PROFITABLE_UNREALISED
        assert grade.max_favourable_excursion > 0

    def test_position_down_is_loss_unrealised(self):
        observations = _obs((1, 88_000), (3, 85_000), (5, 84_000))
        grade = grade_recommendation(
            horizon="24h", evaluated_at=T0,
            buy_price=90_000, sell_target=140_000,
            observations=observations,
        )
        assert grade.outcome_status == LOSS_UNREALISED
        assert grade.realised_roi < 0

    def test_deep_drawdown_is_downside_hit(self):
        observations = _obs((1, 100_000), (3, 85_000), (5, 88_000))
        grade = grade_recommendation(
            horizon="24h", evaluated_at=T0,
            buy_price=100_000, sell_target=140_000,
            observations=observations,
        )
        assert grade.downside_hit is True
        assert grade.outcome_status == DOWNSIDE_HIT
        assert grade.max_adverse_excursion <= -0.10

    def test_flat_outcome_requires_clearing_tax_not_merely_a_flat_price(self):
        # Worth being explicit about, because it is counter-intuitive and
        # it is where naive P&L reporting goes wrong: EA's 5% cut means a
        # NOMINALLY flat price is a real ~5% loss. Breaking even on a
        # 90,000 entry needs a ~94,700 sale, so that - not 90,000 - is
        # what "flat" has to mean here.
        observations = _obs((1, 90_000), (3, 92_000), (5, 94_737))
        grade = grade_recommendation(
            horizon="24h", evaluated_at=T0,
            buy_price=90_000, sell_target=140_000,
            observations=observations,
        )
        assert grade.outcome_status == FLAT
        assert abs(float(grade.realised_roi)) < 0.005

    def test_nominally_unchanged_price_is_recorded_as_a_loss(self):
        observations = _obs((1, 90_000), (3, 90_100), (5, 89_950))
        grade = grade_recommendation(
            horizon="24h", evaluated_at=T0,
            buy_price=90_000, sell_target=140_000,
            observations=observations,
        )
        assert grade.outcome_status == LOSS_UNREALISED
        # ~5% down purely from the tax, before any price move at all.
        assert float(grade.realised_roi) < -0.04


class TestWindowing:
    def test_observations_outside_the_horizon_are_ignored(self):
        # Target is reached, but only after the 24h window closed.
        observations = _obs((1, 88_000), (30, 200_000))
        grade = grade_recommendation(
            horizon="24h", evaluated_at=T0,
            buy_price=90_000, sell_target=115_000,
            observations=observations,
        )
        assert grade.target_hit is False
        assert grade.observation_count == 1

    def test_longer_horizon_sees_the_same_later_observation(self):
        observations = _obs((1, 88_000), (30, 200_000))
        grade = grade_recommendation(
            horizon="7d", evaluated_at=T0,
            buy_price=90_000, sell_target=115_000,
            observations=observations,
        )
        assert grade.target_hit is True
        assert grade.outcome_status == TARGET_HIT

    def test_observation_at_evaluation_time_is_not_evidence(self):
        # The state the call was made FROM is not evidence about what
        # happened next.
        observations = [(88_000, T0), (89_000, T0 + timedelta(hours=1))]
        grade = grade_recommendation(
            horizon="24h", evaluated_at=T0,
            buy_price=90_000, sell_target=115_000,
            observations=observations,
        )
        assert grade.observation_count == 1

    def test_too_few_observations_is_its_own_status(self):
        grade = grade_recommendation(
            horizon="24h", evaluated_at=T0,
            buy_price=90_000, sell_target=115_000,
            observations=_obs((1, 88_000)),
        )
        assert grade.outcome_status == INSUFFICIENT_OBSERVATIONS

    def test_entry_on_the_final_observation_cannot_be_graded(self):
        # Bought on the last print in the window - nothing after it to
        # measure, so the honest answer is "cannot say", not "flat".
        observations = _obs((1, 120_000), (2, 88_000))
        grade = grade_recommendation(
            horizon="24h", evaluated_at=T0,
            buy_price=90_000, sell_target=115_000,
            observations=observations,
        )
        assert grade.entry_achieved is True
        assert grade.outcome_status == INSUFFICIENT_OBSERVATIONS


class TestProvenance:
    def test_grade_is_versioned(self):
        grade = grade_recommendation(
            horizon="24h", evaluated_at=T0, buy_price=90_000,
            sell_target=115_000, observations=_obs((1, 88_000), (2, 130_000)),
        )
        assert grade.grader_version.startswith("grader-")
        assert set(grade.as_dict()) >= {
            "entry_achieved", "exit_achieved", "realised_roi",
            "max_favourable_excursion", "max_adverse_excursion",
            "outcome_status", "grader_version",
        }
