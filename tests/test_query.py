"""The query sent to ERIC, and the language names used to filter what comes back."""

from __future__ import annotations

import pytest

from screener.query import filter_for, language_names, search_query


class TestSearchQuery:
    def test_it_is_sent_exactly_as_written(self):
        """So a strategy returns what it returns on eric.ed.gov. ERIC's search is Solr:
        quoted phrases, AND/OR/NOT and parentheses need no translation."""
        query = '("board game" OR chess) AND cognitive NOT digital'
        assert search_query(query) == query

    def test_field_names_and_commas_are_left_alone(self):
        query = 'title:chess AND subject:"Educational Games" AND "games, play and learning"'
        assert search_query(query) == query

    def test_multiline_yaml_query_is_collapsed_to_one_line(self):
        assert search_query('("board game"\n   OR chess)\nAND cognitive') == (
            '("board game" OR chess) AND cognitive'
        )

    def test_empty_query_is_rejected(self):
        with pytest.raises(ValueError, match="empty"):
            search_query("   ")

    def test_the_protocols_filters_are_not_added_to_it(self, protocol):
        """Filters are applied to the fetched records instead; see test_eric.TestFilters."""
        assert filter_for(protocol) == protocol.query


class TestLanguageNames:
    def test_codes_become_the_names_eric_uses(self):
        assert language_names(["en", " IT ", "fr"]) == ["English", "Italian", "French"]

    def test_an_eric_language_name_is_accepted_as_is(self):
        assert language_names(["Norwegian"]) == ["Norwegian"]

    def test_duplicates_collapse_but_order_is_kept(self):
        assert language_names(["it", "en", "IT"]) == ["Italian", "English"]

    def test_blank_entries_are_ignored(self):
        assert language_names(["", "  "]) == []


def test_real_protocol_query_is_one_line_and_balanced(real_protocol):
    result = filter_for(real_protocol)
    assert "\n" not in result
    assert result.startswith('("board game" OR')
    assert result.count("(") == result.count(")")
