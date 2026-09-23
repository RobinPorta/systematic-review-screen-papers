"""The decision policy, exercised without touching the network.

Everything here is arithmetic over probabilities, which is exactly why building on a
decision model is worth it: the part of the system that can get a systematic review wrong
is a pure function with a truth table.
"""

from __future__ import annotations

import pytest

from screener.answers import ChoiceValue, NoulValue
from screener.decide import Label, Verdict, decide
from screener.questions import EXCLUSION_REASON_ID, NO_SINGLE_REASON

from .conftest import answers


class TestHappyPaths:
    def test_all_inclusions_met_and_nothing_excludes_is_include(self, protocol):
        assert decide(protocol, answers()).label is Label.INCLUDE

    def test_a_hard_exclusion_drops_the_record(self, protocol):
        decision = decide(protocol, answers(exc_digital=0.95))
        assert decision.label is Label.EXCLUDE

    def test_an_unmet_inclusion_goes_to_a_human_not_to_exclude(self, protocol):
        """The asymmetry that makes this recall-safe: failing to prove eligibility is not
        the same as proving ineligibility."""
        assert decide(protocol, answers(inc_humans=0.2)).label is Label.MAYBE


class TestThresholdBoundaries:
    def test_exclusion_just_below_the_bar_does_not_fire(self, protocol):
        assert decide(protocol, answers(exc_digital=0.84)).label is Label.INCLUDE

    def test_exclusion_exactly_on_the_bar_fires(self, protocol):
        assert decide(protocol, answers(exc_digital=0.85)).label is Label.EXCLUDE

    def test_inclusion_exactly_on_the_bar_counts_as_met(self, protocol):
        assert decide(protocol, answers(inc_humans=0.60)).label is Label.INCLUDE

    def test_inclusion_just_below_the_bar_does_not(self, protocol):
        assert decide(protocol, answers(inc_humans=0.59)).label is Label.MAYBE

    def test_a_middling_exclusion_alone_never_drops_a_record(self, protocol):
        """0.7 on an exclusion is real evidence but not enough to act on unsupervised."""
        decision = decide(protocol, answers(exc_digital=0.7))
        assert decision.label is Label.INCLUDE
        reason = next(r for r in decision.reasons if r.criterion_id == "exc_digital")
        assert reason.verdict is Verdict.NOT_MET


class TestGuards:
    def test_an_uninformative_abstract_goes_to_a_human(self, protocol):
        assert decide(protocol, answers(informative=0.1)).label is Label.MAYBE

    def test_an_uninformative_abstract_does_not_block_a_clear_exclusion(self, protocol):
        """If a criterion fires hard, the record can be dropped even from a thin abstract —
        the exclusion is positive evidence, not an absence of it."""
        decision = decide(protocol, answers(informative=0.1, exc_gambling=0.99))
        assert decision.label is Label.EXCLUDE


class TestMissingAnswers:
    def test_a_missing_inclusion_answer_never_yields_include(self, protocol):
        values = answers()
        del values["inc_played"]
        assert decide(protocol, values).label is Label.MAYBE

    def test_a_missing_answer_also_blocks_an_automatic_exclusion(self, protocol):
        """A partial response is not grounds for dropping a study on its own."""
        values = answers(exc_digital=0.99)
        del values["inc_played"]
        assert decide(protocol, values).label is Label.MAYBE


class TestAuditTrail:
    def test_every_criterion_appears_with_its_probability(self, protocol):
        decision = decide(protocol, answers())
        assert {r.criterion_id for r in decision.reasons} == {
            "informative", "inc_humans", "inc_played", "exc_digital", "exc_gambling"
        }

    def test_deciding_reasons_on_an_exclusion_are_the_ones_that_fired(self, protocol):
        decision = decide(protocol, answers(exc_gambling=0.97))
        assert [r.criterion_id for r in decision.deciding_reasons] == ["exc_gambling"]

    def test_sections_read_are_recorded_for_stage_two(self, protocol):
        decision = decide(
            protocol,
            answers(),
            sections_read={"inc_humans": ["methods", "participants"]},
        )
        reason = next(r for r in decision.reasons if r.criterion_id == "inc_humans")
        assert reason.sections == ["methods", "participants"]
        assert "read: methods, participants" in reason.describe()


class TestRanking:
    def test_rank_normalises_the_score_onto_zero_to_one(self, protocol):
        """A three-level scale returns 0..2, so a top score must come back as 1.0."""
        assert decide(protocol, answers(topical_fit=2.0)).rank == 1.0
        assert decide(protocol, answers(topical_fit=1.0)).rank == 0.5
        assert decide(protocol, answers(topical_fit=0.0)).rank == 0.0

    def test_rank_falls_back_to_mean_inclusion_probability(self, protocol):
        values = answers(inc_humans=0.8, inc_played=0.4)
        del values["topical_fit"]
        assert decide(protocol, values).rank == 0.6


class TestExclusionReasons:
    def test_jevs_choice_supplies_the_prisma_reason(self, protocol):
        decision = decide(protocol, answers(
            exc_digital=0.95,
            **{EXCLUSION_REASON_ID: ChoiceValue(
                choice="exc_digital", confidence=0.8, probabilities={"exc_digital": 0.9}
            )},
        ))
        assert decision.exclusion_reason == "Digital games only"
        assert decision.exclusion_reason_confidence == 0.8

    def test_a_low_confidence_choice_is_ignored_for_the_fired_criterion(self, protocol):
        decision = decide(protocol, answers(
            exc_gambling=0.99,
            **{EXCLUSION_REASON_ID: ChoiceValue(
                choice="exc_digital", confidence=0.05, probabilities={}
            )},
        ))
        assert decision.exclusion_reason == "Gambling context"
        assert decision.exclusion_reason_confidence is None

    def test_no_single_reason_falls_back_to_the_criterion_that_fired_hardest(self, protocol):
        decision = decide(protocol, answers(
            exc_digital=0.90,
            exc_gambling=0.99,
            **{EXCLUSION_REASON_ID: ChoiceValue(
                choice=NO_SINGLE_REASON, confidence=0.9, probabilities={}
            )},
        ))
        assert decision.exclusion_reason == "Gambling context"

    def test_included_records_carry_no_exclusion_reason(self, protocol):
        assert decide(protocol, answers()).exclusion_reason is None


class TestNoulUncertainty:
    def test_a_coin_flip_is_maximally_uncertain(self):
        assert NoulValue(noul=0.5).uncertainty == 1.0

    def test_both_extremes_are_certain(self):
        assert NoulValue(noul=0.0).uncertainty == 0.0
        assert NoulValue(noul=1.0).uncertainty == 0.0

    def test_uncertainty_is_symmetric_about_a_half(self):
        assert NoulValue(noul=0.3).uncertainty == pytest.approx(NoulValue(noul=0.7).uncertainty)
