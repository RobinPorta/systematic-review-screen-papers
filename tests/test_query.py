from __future__ import annotations

import pytest

from screener.protocol import Filters
from screener.query import (
    build_filter,
    date_filters,
    filter_for,
    language_filter,
    type_filter,
)


class TestDateFilters:
    def test_from_year_becomes_an_inclusive_january_first(self):
        """OpenAlex date bounds are inclusive, so 1990 onwards is simply 1990-01-01 —
        no exclusive-operator off-by-one to get wrong, unlike Scopus' PUBYEAR AFT."""
        assert date_filters(1990, None) == ["from_publication_date:1990-01-01"]

    def test_to_year_becomes_an_inclusive_december_thirty_first(self):
        assert date_filters(None, 2026) == ["to_publication_date:2026-12-31"]

    def test_both_bounds(self):
        assert date_filters(1990, 2026) == [
            "from_publication_date:1990-01-01",
            "to_publication_date:2026-12-31",
        ]

    def test_no_bounds_produces_no_filter(self):
        assert date_filters(None, None) == []

    def test_a_single_year_range_spans_that_whole_year(self):
        assert date_filters(2000, 2000) == [
            "from_publication_date:2000-01-01",
            "to_publication_date:2000-12-31",
        ]


class TestLanguageFilter:
    def test_multiple_codes_are_ored_with_a_pipe(self):
        assert language_filter(["en", "it"]) == "language:en|it"

    def test_case_and_padding_are_normalised(self):
        assert language_filter(["  EN ", "It"]) == "language:en|it"

    def test_duplicates_collapse_but_order_is_kept(self):
        assert language_filter(["it", "en", "it"]) == "language:it|en"

    def test_empty_produces_no_filter(self):
        assert language_filter([]) is None
        assert language_filter(["", "  "]) is None


class TestTypeFilter:
    def test_types_are_ored(self):
        assert type_filter(["article", "review"]) == "type:article|review"

    def test_empty_produces_no_filter(self):
        assert type_filter([]) is None


class TestBuildFilter:
    def test_all_clauses_are_comma_separated(self):
        result = build_filter(
            '"board game"',
            Filters(year_from=1990, year_to=2026, languages=["en", "it"], types=["article"]),
        )
        assert result == (
            'title_and_abstract.search:"board game",'
            "from_publication_date:1990-01-01,"
            "to_publication_date:2026-12-31,"
            "language:en|it,"
            "type:article"
        )

    def test_no_filters_leaves_just_the_search(self):
        assert build_filter('"board game"', Filters()) == 'title_and_abstract.search:"board game"'

    def test_require_abstract_adds_its_clause_only_when_asked(self):
        assert "has_abstract:true" not in build_filter("x", Filters())
        assert "has_abstract:true" in build_filter("x", Filters(require_abstract=True))

    def test_a_comma_in_the_query_is_rejected_rather_than_truncating_the_search(self):
        """OpenAlex reads a comma as a filter separator, so an unnoticed one would silently
        cut the search strategy in half — a quiet way to corrupt a review."""
        with pytest.raises(ValueError, match="comma"):
            build_filter('"board game", chess', Filters())

    def test_multiline_yaml_query_is_collapsed_to_one_line(self):
        """A folded YAML scalar keeps newlines around its more-indented lines."""
        result = build_filter('("a" OR "b")\n  AND c', Filters())
        assert "\n" not in result
        assert result == 'title_and_abstract.search:("a" OR "b") AND c'

    def test_empty_query_is_rejected(self):
        with pytest.raises(ValueError, match="empty"):
            build_filter("   ", Filters())

    def test_boolean_syntax_is_passed_through_untouched(self):
        """OpenAlex supports quoted phrases, AND/OR/NOT and parentheses natively, so the
        protocol's query needs no translation."""
        query = '("board game" OR chess) AND cognitive NOT digital'
        assert build_filter(query, Filters()).endswith(query)

    def test_filter_for_uses_the_protocols_own_filters(self, protocol):
        result = filter_for(protocol)
        assert "from_publication_date:2000-01-01" in result
        assert "language:en" in result
        assert "type:article" in result


def test_real_protocol_filter_is_wellformed(real_protocol):
    result = filter_for(real_protocol)
    assert "\n" not in result
    assert result.startswith("title_and_abstract.search:")
    assert "from_publication_date:1990-01-01" in result
    assert "language:en|it" in result
    assert "type:article|review" in result

    search = result.split(",")[0]
    assert search.count("(") == search.count(")"), "unbalanced parentheses in the query"
