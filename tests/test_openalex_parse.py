"""Parsing and filtering OpenAlex works, against a saved response. No network."""

from __future__ import annotations

import json

from screener.protocol import Filters
from screener.records import (
    abstract_from_inverted_index,
    apply_filters,
    dedupe,
    from_openalex_work,
    parse_year,
)

from .conftest import FIXTURES


def load_works() -> list[dict]:
    return json.loads((FIXTURES / "openalex_page.json").read_text(encoding="utf-8"))["results"]


class TestInvertedIndex:
    """OpenAlex publishes abstracts as {word: [positions]} rather than running text."""

    def test_word_order_is_recovered_from_the_positions(self):
        index = {"the": [0, 3], "board": [1], "game": [2], "study": [4]}
        assert abstract_from_inverted_index(index) == "the board game the study"

    def test_a_repeated_word_lands_in_every_position(self):
        assert abstract_from_inverted_index({"a": [0, 2], "b": [1]}) == "a b a"

    def test_gaps_in_the_positions_do_not_break_it(self):
        assert abstract_from_inverted_index({"x": [0], "y": [7]}) == "x y"

    def test_missing_or_empty_gives_an_empty_string(self):
        assert abstract_from_inverted_index(None) == ""
        assert abstract_from_inverted_index({}) == ""

    def test_the_fixture_abstract_reads_as_prose(self):
        record = from_openalex_work(load_works()[0])
        assert record.abstract.startswith("Background: Loneliness is common")
        assert "UCLA Loneliness Scale" in record.abstract


class TestNormalisation:
    def test_core_fields_are_unpacked(self):
        record = from_openalex_work(load_works()[0])
        assert record.id == "W2100000001"
        assert record.doi == "10.1016/j.jas.2021.100001"
        assert record.title.startswith("Weekly board-game sessions")
        assert record.journal == "Journal of Ageing and Society"
        assert record.year == 2021
        assert record.work_type == "article"
        assert record.language == "en"
        assert record.authors == "Marco Rossi"
        assert record.is_oa is True

    def test_the_doi_url_prefix_is_stripped(self):
        """OpenAlex returns DOIs as https://doi.org/... — downstream wants the bare DOI."""
        assert not (from_openalex_work(load_works()[0]).doi or "").startswith("http")

    def test_pmcid_is_carried_through_for_stage_two(self):
        """Saves a Europe PMC lookup per paper when OpenAlex already knows the id."""
        record = from_openalex_work(load_works()[0])
        assert record.pmcid == "PMC8000001"
        assert record.pmid == "34000001"

    def test_keywords_are_flattened_to_strings(self):
        assert from_openalex_work(load_works()[0]).keywords == [
            "board games", "loneliness", "older adults"
        ]

    def test_a_work_missing_venue_authors_and_abstract_still_parses(self):
        """OpenAlex nulls fields freely; a sparse record is still screenable."""
        record = from_openalex_work(load_works()[2])
        assert record.id == "W2100000003"
        assert record.doi is None
        assert record.journal == ""
        assert record.authors == ""
        assert record.abstract == ""
        assert record.has_abstract is False

    def test_an_almost_empty_work_does_not_raise(self):
        record = from_openalex_work({"display_name": "A title"})
        assert record.title == "A title"
        assert record.year is None


class TestState:
    def test_state_carries_what_the_judgement_needs(self):
        state = from_openalex_work(load_works()[0]).state()
        assert set(state) == {"title", "abstract", "keywords", "journal", "publication_type"}

    def test_state_omits_the_year(self):
        """Deliberate: the year is enforced by the filter and in code, and Jev is documented
        as unreliable at comparing dates. Putting it in the state would only invite a
        question that should never be asked."""
        assert "year" not in from_openalex_work(load_works()[0]).state()

    def test_state_omits_identifiers_authors_and_oa_status(self):
        state = from_openalex_work(load_works()[0]).state()
        for noise in ("doi", "id", "authors", "pmcid", "is_oa"):
            assert noise not in state


class TestYearParsing:
    def test_iso_date(self):
        assert parse_year("2021-06-01") == 2021

    def test_missing_date(self):
        assert parse_year(None) is None
        assert parse_year("") is None


class TestFilters:
    def test_year_filter_drops_an_out_of_range_record(self):
        records = [from_openalex_work(w) for w in load_works()]
        outcome = apply_filters(records, Filters(year_from=1990))
        assert len(outcome.kept) == 2
        assert any("before 1990" in reason for _, reason in outcome.dropped)

    def test_type_allowlist_drops_a_preprint(self):
        records = [from_openalex_work(w) for w in load_works()]
        outcome = apply_filters(records, Filters(types=["article"]))
        assert {r.work_type for r in outcome.kept} == {"article"}

    def test_language_really_is_rechecked_unlike_a_scopus_clause(self):
        """OpenAlex returns `language` on every work, so this filter is verifiable."""
        records = [from_openalex_work(w) for w in load_works()]
        outcome = apply_filters(records, Filters(languages=["en"]))
        assert len(outcome.kept) == 2
        assert any("language de" in reason for _, reason in outcome.dropped)

    def test_a_work_with_no_language_recorded_is_kept(self):
        """OpenAlex leaves `language` null often enough that dropping on it would lose
        eligible studies over a metadata gap."""
        records = [from_openalex_work({"display_name": "No language", "publication_year": 2020})]
        assert len(apply_filters(records, Filters(languages=["en"])).kept) == 1

    def test_records_without_abstracts_are_never_dropped_here(self):
        """Absence of an abstract is not evidence of ineligibility. These reach the
        informative guard and land in `maybe` for a human, as PRISMA expects."""
        records = [from_openalex_work(load_works()[2])]
        assert len(apply_filters(records, Filters()).kept) == 1

    def test_no_filters_keeps_everything(self):
        records = [from_openalex_work(w) for w in load_works()]
        assert len(apply_filters(records, Filters()).kept) == 3

    def test_drop_reasons_are_tallied(self):
        records = [from_openalex_work(w) for w in load_works()]
        outcome = apply_filters(records, Filters(year_from=1990, types=["article"]))
        assert sum(outcome.drop_reasons.values()) == 1


class TestDedupe:
    def test_repeated_doi_is_removed(self):
        records = [from_openalex_work(w) for w in load_works()]
        assert len(dedupe(records + records)) == 3

    def test_titles_match_across_punctuation_and_case(self):
        a = from_openalex_work({"display_name": "Board Games, and Ageing"})
        b = from_openalex_work({"display_name": "board games and ageing"})
        assert len(dedupe([a, b])) == 1

    def test_different_records_are_both_kept(self):
        a = from_openalex_work({"display_name": "One", "doi": "https://doi.org/10.1/a"})
        b = from_openalex_work({"display_name": "Two", "doi": "https://doi.org/10.1/b"})
        assert len(dedupe([a, b])) == 2
