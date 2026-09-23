"""Stage-2 bundling: routing criteria to sections, and fitting inside the context budget."""

from __future__ import annotations

from screener.answers import ChoiceValue, NoulValue, ScoreValue
from screener.fulltext.sections import ParsedSection, ParsedSections, parse_sections
from screener.protocol import Section
from screener.questions import stage1_questions, stage2_bundles
from screener.screen_fulltext import MAX_STATE_TOKENS, build_parts, merge_answers

from .conftest import FIXTURES


def fixture_paper() -> ParsedSections:
    return parse_sections((FIXTURES / "europepmc_article.xml").read_text(encoding="utf-8"))


class TestBundling:
    def test_questions_sharing_a_routing_share_a_call(self, protocol):
        bundles = stage2_bundles(protocol)
        by_key = {b.key: set(b.questions) for b in bundles}
        assert by_key["methods+participants"] == {"inc_humans", "design"}
        assert by_key["methods+intervention"] == {"inc_played", "exc_digital", "exc_gambling"}

    def test_every_question_lands_in_exactly_one_bundle(self, protocol):
        bundles = stage2_bundles(protocol)
        placed = [qid for bundle in bundles for qid in bundle.questions]
        assert sorted(placed) == sorted(stage1_questions(protocol))
        assert len(placed) == len(set(placed))

    def test_the_real_protocol_collapses_to_a_handful_of_calls(self, real_protocol):
        """Sixteen questions, four requests. The whole point of routing."""
        bundles = stage2_bundles(real_protocol)
        assert len(bundles) <= 5, f"{len(bundles)} bundles is too many calls per paper"

    def test_duplicate_sections_in_a_routing_are_collapsed(self):
        from screener.protocol import Criterion, Protocol

        spec = Protocol(
            name="dup",
            query="x",
            criteria=[
                Criterion(
                    id="a", kind="inclusion", label="A", instructions="?",
                    sections=[Section.METHODS, Section.METHODS, Section.RESULTS],
                )
            ],
        )
        assert stage2_bundles(spec)[0].sections == [Section.METHODS, Section.RESULTS]


class TestBuildParts:
    def test_a_normal_bundle_carries_its_sections(self, protocol):
        bundle = next(b for b in stage2_bundles(protocol) if b.key == "methods+participants")
        parts = build_parts(bundle, fixture_paper())
        assert len(parts) == 1
        assert {"methods", "participants"} <= set(parts[0].state)

    def test_every_state_is_anchored_with_title_and_abstract(self, protocol):
        """Without this, a bundle whose sections are absent would send less than stage 1
        already had from the abstract — a full-text stage producing worse answers on more
        text."""
        bundle = next(b for b in stage2_bundles(protocol) if b.key == "methods+participants")
        parts = build_parts(bundle, fixture_paper())
        assert {"title", "abstract"} <= set(parts[0].state)

    def test_missing_sections_fall_back_to_the_body_not_just_the_abstract(self, protocol):
        """The bug this caught on real papers: a paper whose headings resist labelling had
        its whole parsed body discarded, and stage 2 quietly re-screened the abstract."""
        bundle = next(b for b in stage2_bundles(protocol) if b.key == "methods+participants")
        paper = ParsedSections(
            title="A title",
            abstract="An abstract",
            sections=[
                ParsedSection(title="Comparison studies", text="We recruited forty adults.",
                              canonical=Section.INTRODUCTION),
                ParsedSection(title="Implications", text="The effect persisted.",
                              canonical=Section.DISCUSSION),
            ],
        )
        parts = build_parts(bundle, paper)
        assert set(parts[0].state) == {"title", "abstract", "body"}
        assert "forty adults" in parts[0].state["body"]
        assert "The effect persisted" in parts[0].state["body"]

    def test_the_fallback_body_skips_front_and_back_matter(self, protocol):
        bundle = next(b for b in stage2_bundles(protocol) if b.key == "methods+participants")
        paper = ParsedSections(
            title="T", abstract="A",
            sections=[
                ParsedSection(title="Real content", text="Forty adults took part.",
                              canonical=Section.INTRODUCTION),
                ParsedSection(title="Acknowledgements", text="We thank the funders.",
                              canonical=Section.OTHER),
            ],
        )
        body = build_parts(bundle, paper)[0].state["body"]
        assert "Forty adults" in body
        assert "thank the funders" not in body

    def test_only_title_and_abstract_is_still_a_valid_state(self, protocol):
        bundle = next(b for b in stage2_bundles(protocol) if b.key == "methods+participants")
        bare = ParsedSections(title="A title", abstract="An abstract", sections=[])
        assert set(build_parts(bundle, bare)[0].state) == {"title", "abstract"}

    def test_a_paper_with_nothing_at_all_produces_no_request(self, protocol):
        bundle = stage2_bundles(protocol)[0]
        assert build_parts(bundle, ParsedSections()) == []

    def test_an_oversized_bundle_splits_on_section_boundaries(self, protocol):
        bundle = next(b for b in stage2_bundles(protocol) if b.key == "methods+participants")
        big = "word " * (MAX_STATE_TOKENS * 2)
        paper = ParsedSections(sections=[
            ParsedSection(title="Methods", text=big, canonical=Section.METHODS),
            ParsedSection(title="Participants", text=big, canonical=Section.PARTICIPANTS),
        ])
        parts = build_parts(bundle, paper)
        assert len(parts) > 1
        assert all(part.tokens <= MAX_STATE_TOKENS for part in parts)

    def test_a_single_oversized_section_is_truncated_with_a_marker(self, protocol):
        bundle = next(b for b in stage2_bundles(protocol) if b.key == "methods+participants")
        paper = ParsedSections(sections=[
            ParsedSection(title="Methods", text="word " * (MAX_STATE_TOKENS * 3),
                          canonical=Section.METHODS),
        ])
        parts = build_parts(bundle, paper)
        assert len(parts) == 1
        assert parts[0].tokens <= MAX_STATE_TOKENS
        assert "truncated" in parts[0].state["methods"]

    def test_split_parts_get_distinct_keys(self, protocol):
        bundle = next(b for b in stage2_bundles(protocol) if b.key == "methods+participants")
        big = "word " * (MAX_STATE_TOKENS * 2)
        paper = ParsedSections(sections=[
            ParsedSection(title="M", text=big, canonical=Section.METHODS),
            ParsedSection(title="P", text=big, canonical=Section.PARTICIPANTS),
        ])
        keys = [part.bundle_key for part in build_parts(bundle, paper)]
        assert len(keys) == len(set(keys))

    def test_no_request_ever_exceeds_the_budget(self, real_protocol):
        """The pre-flight guarantee: a 400 mid-run is a bad way to learn the state was big."""
        paper = fixture_paper()
        for bundle in stage2_bundles(real_protocol):
            for part in build_parts(bundle, paper):
                assert part.tokens <= MAX_STATE_TOKENS


class TestMergeAnswers:
    def test_a_single_answer_passes_straight_through(self):
        merged = merge_answers({"a": [NoulValue(noul=0.7)]})
        assert merged["a"].noul == 0.7

    def test_nouls_take_the_maximum_across_parts(self):
        """These criteria ask whether the paper *contains* some evidence, so a yes anywhere
        in the text is a yes."""
        merged = merge_answers({"a": [NoulValue(noul=0.2), NoulValue(noul=0.9), NoulValue(noul=0.4)]})
        assert merged["a"].noul == 0.9

    def test_choices_take_the_most_confident_answer(self):
        merged = merge_answers({"d": [
            ChoiceValue(choice="rct", confidence=0.3, probabilities={}),
            ChoiceValue(choice="cohort", confidence=0.9, probabilities={}),
        ]})
        assert merged["d"].choice == "cohort"

    def test_scores_take_the_most_confident_answer(self):
        merged = merge_answers({"s": [
            ScoreValue(score=0.5, confidence=0.2),
            ScoreValue(score=2.0, confidence=0.95),
        ]})
        assert merged["s"].score == 2.0

    def test_an_empty_list_is_skipped_rather_than_invented(self):
        assert merge_answers({"a": []}) == {}
