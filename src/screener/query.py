"""Assemble an OpenAlex query from the protocol's search string plus its filters.

Years, languages and work types are filtered *here* and re-checked in `records.py`, never
by asking Jev. Jev's own jaggedness notes call out date comparison and numeric reasoning as
unreliable, and a search index does this exactly and for free.

OpenAlex takes a comma-separated `filter` parameter where commas mean AND and `|` means OR
within one field. The search string itself supports quoted phrases, `AND`, `OR`, `NOT` and
parentheses — so a protocol's topical query can be written much as it would be in any
database, and is passed through unchanged.
"""

from __future__ import annotations

import re

from .protocol import Filters, Protocol

_WHITESPACE = re.compile(r"\s+")

#: The field the topical query searches. Title and abstract together is the standard scope
#: for a systematic review's search strategy.
SEARCH_FIELD = "title_and_abstract.search"


def date_filters(year_from: int | None, year_to: int | None) -> list[str]:
    """Build the publication-date filters.

    OpenAlex's `from_publication_date` / `to_publication_date` are **inclusive** and take
    full ISO dates, so 1990 onwards is simply `1990-01-01` — no off-by-one to get wrong.
    """
    filters: list[str] = []
    if year_from is not None:
        filters.append(f"from_publication_date:{year_from:04d}-01-01")
    if year_to is not None:
        filters.append(f"to_publication_date:{year_to:04d}-12-31")
    return filters


def language_filter(languages: list[str]) -> str | None:
    """Build the language filter.

    OpenAlex uses two-letter ISO 639-1 codes, and several values in one field are ORed
    with `|`. Unlike a Scopus `LANGUAGE()` clause, this *is* verifiable afterwards: every
    work carries a `language` field, so `records.apply_filters` re-checks it.
    """
    codes = _unique(lang.strip().lower() for lang in languages)
    return f"language:{'|'.join(codes)}" if codes else None


def type_filter(types: list[str]) -> str | None:
    """Build the work-type filter, e.g. `type:article|review`."""
    values = _unique(value.strip().lower() for value in types)
    return f"type:{'|'.join(values)}" if values else None


def _unique(values) -> list[str]:
    seen: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.append(value)
    return seen


def build_filter(topical: str, filters: Filters) -> str:
    """Combine the topical search with every filter clause into one `filter` value."""
    # A YAML folded scalar keeps the newlines around its more-indented lines, so a query
    # written across several lines for readability arrives here with them still in.
    topical = _WHITESPACE.sub(" ", topical).strip()
    if not topical:
        raise ValueError("the protocol's topical query is empty")
    if "," in topical:
        # A comma would be read as a filter separator and silently truncate the search.
        raise ValueError(
            "the topical query cannot contain a comma — OpenAlex reads it as a filter "
            "separator. Use OR instead."
        )

    clauses = [f"{SEARCH_FIELD}:{topical}"]
    clauses.extend(date_filters(filters.year_from, filters.year_to))
    for clause in (language_filter(filters.languages), type_filter(filters.types)):
        if clause:
            clauses.append(clause)
    if filters.require_abstract:
        clauses.append("has_abstract:true")
    return ",".join(clauses)


def filter_for(protocol: Protocol) -> str:
    return build_filter(protocol.query, protocol.filters)
