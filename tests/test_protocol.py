"""Protocol loading, validation, and the mapping onto Jev question objects."""

from __future__ import annotations

import pytest
from typesafe_sdk import Choice, Noul, Score

from screener.protocol import Protocol, Section
from screener.questions import (
    EXCLUSION_REASON_ID,
    NO_SINGLE_REASON,
    exclusion_reason_question,
    section_label_question,
    stage1_questions,
)

from .conftest import TINY


class TestValidation:
    def test_duplicate_question_ids_are_rejected(self):
        spec = {**TINY, "choices": [
            {"id": "inc_humans", "instructions": "?", "options": {"a": None, "b": None}}
        ]}
        with pytest.raises(ValueError, match="duplicate question ids"):
            Protocol.model_validate(spec)

    def test_a_protocol_with_no_inclusion_criteria_is_rejected(self):
        spec = {**TINY, "criteria": [c for c in TINY["criteria"] if c["kind"] != "inclusion"]}
        with pytest.raises(ValueError, match="at least one inclusion"):
            Protocol.model_validate(spec)

    def test_a_choice_needs_two_options(self):
        spec = {**TINY, "choices": [{"id": "x", "instructions": "?", "options": {"only": None}}]}
        with pytest.raises(ValueError, match="at least two options"):
            Protocol.model_validate(spec)

    def test_a_score_is_capped_at_ten_levels(self):
        spec = {**TINY, "scores": [
            {"id": "x", "instructions": "?", "levels": [str(i) for i in range(11)]}
        ]}
        with pytest.raises(ValueError, match="between 2 and 10"):
            Protocol.model_validate(spec)

    def test_inverted_year_bounds_are_rejected(self):
        spec = {**TINY, "filters": {"year_from": 2020, "year_to": 2010}}
        with pytest.raises(ValueError, match="after year_to"):
            Protocol.model_validate(spec)

    def test_a_threshold_outside_zero_to_one_is_rejected(self):
        spec = {**TINY, "thresholds": {"exclude_min": 1.5}}
        with pytest.raises(ValueError, match="between 0 and 1"):
            Protocol.model_validate(spec)


class TestQuestionMapping:
    def test_each_criterion_kind_becomes_the_right_sdk_type(self, protocol):
        questions = stage1_questions(protocol)
        assert isinstance(questions["inc_humans"], Noul)
        assert isinstance(questions["design"], Choice)
        assert isinstance(questions["topical_fit"], Score)

    def test_every_protocol_question_is_asked_in_one_stage_one_call(self, real_protocol):
        questions = stage1_questions(real_protocol)
        expected = (
            len(real_protocol.criteria) + len(real_protocol.choices) + len(real_protocol.scores)
        )
        assert len(questions) == expected

    def test_outcome_descriptions_become_noul_criteria(self, real_protocol):
        question = stage1_questions(real_protocol)["exc_game_as_instrument"]
        assert question.criteria is not None
        assert "true" in question.criteria and "false" in question.criteria
        assert "Prisoner" in str(question.criteria["true"])

    def test_a_criterion_without_outcomes_sends_no_criteria(self, real_protocol):
        assert stage1_questions(real_protocol)["exc_gambling"].criteria is None

    def test_score_levels_keep_their_order(self, protocol):
        assert stage1_questions(protocol)["topical_fit"].criteria == [
            "incidental", "partial", "central"
        ]

    def test_top_level_is_one_less_than_the_level_count(self, protocol):
        assert protocol.score_spec("topical_fit").top_level == 2


class TestSyntheticQuestions:
    def test_the_exclusion_reason_offers_every_exclusion_plus_an_escape(self, protocol):
        options = exclusion_reason_question(protocol).criteria
        assert set(options) == {"exc_digital", "exc_gambling", NO_SINGLE_REASON}

    def test_the_reason_options_are_labelled_for_humans(self, protocol):
        assert exclusion_reason_question(protocol).criteria["exc_digital"] == "Digital games only"

    def test_the_section_classifier_covers_the_canonical_sections(self):
        options = set(section_label_question().criteria)
        body_sections = {s.value for s in Section} - {Section.TITLE.value, Section.ABSTRACT.value}
        assert options == body_sections

    def test_the_reason_question_id_is_not_a_protocol_id(self, real_protocol):
        assert real_protocol.criterion(EXCLUSION_REASON_ID) is None


class TestRoundTrip:
    def test_saving_and_reloading_preserves_the_protocol(self, real_protocol, tmp_path):
        target = tmp_path / "saved.yaml"
        real_protocol.save(target)
        reloaded = Protocol.load(target)

        assert reloaded.name == real_protocol.name
        assert reloaded.query == real_protocol.query
        assert reloaded.filters == real_protocol.filters
        assert reloaded.thresholds == real_protocol.thresholds
        assert [c.id for c in reloaded.criteria] == [c.id for c in real_protocol.criteria]

    def test_yes_and_no_descriptions_survive_a_round_trip(self, real_protocol, tmp_path):
        """YAML turns an unquoted `true:` key into a boolean, so these have to stay quoted."""
        target = tmp_path / "saved.yaml"
        real_protocol.save(target)
        criterion = Protocol.load(target).criterion("exc_game_as_instrument")
        assert criterion.outcomes is not None
        assert "Prisoner" in str(criterion.outcomes.as_criteria()["true"])

    def test_edited_thresholds_persist(self, real_protocol, tmp_path):
        target = tmp_path / "saved.yaml"
        real_protocol.thresholds.exclude_min = 0.77
        real_protocol.save(target)
        assert Protocol.load(target).thresholds.exclude_min == 0.77


def test_the_shipped_protocol_is_valid(real_protocol):
    assert real_protocol.inclusions and real_protocol.exclusions and real_protocol.guards
    for criterion in real_protocol.criteria:
        assert criterion.instructions.strip(), f"{criterion.id} has no instructions"
        assert criterion.label.strip(), f"{criterion.id} has no human-readable label"
