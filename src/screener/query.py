"""The query sent to ERIC: the protocol's topical query, exactly as written.

Nothing is added to it. A search strategy has to be reportable and repeatable, so the
query this project sends returns what the same query returns on eric.ed.gov. ERIC's
`search` parameter is a Solr query: quoted phrases, `AND`, `OR`, `NOT`, parentheses and
`field:(…)` all work. With no field names, ERIC matches each term in the title, abstract,
descriptors, source or author, and matches word forms exactly — which is why a strategy
written for ERIC spells out its plurals. To narrow or filter at the source, write it into
the query: `title:(…)`, `subject:"Educational Games"`, `publicationdateyear:[2000 TO *]`.

The protocol's filters — years, languages, publication types, peer review — are applied in
code after fetching (`records.apply_filters`), never by asking Jev: its own jaggedness
notes call out date comparison and numeric reasoning as unreliable, and every field needed
comes back on the record.
"""

from __future__ import annotations

import re

from .protocol import Protocol

_WHITESPACE = re.compile(r"\s+")

#: ERIC records a language by name. Protocols use ISO 639-1 codes; anything not listed here
#: is taken to be an ERIC language name already.
LANGUAGE_NAMES = {
    "en": "English", "it": "Italian", "fr": "French", "de": "German", "es": "Spanish",
    "pt": "Portuguese", "nl": "Dutch", "zh": "Chinese", "ja": "Japanese", "ru": "Russian",
    "ar": "Arabic", "tr": "Turkish", "el": "Greek", "sv": "Swedish", "ko": "Korean",
}


def language_names(languages: list[str]) -> list[str]:
    """Protocol language codes as the names ERIC uses, in order, without repeats."""
    names: list[str] = []
    for value in languages:
        cleaned = value.strip()
        name = LANGUAGE_NAMES.get(cleaned.lower(), cleaned.title())
        if name and name not in names:
            names.append(name)
    return names


def search_query(topical: str) -> str:
    """The topical query as ERIC receives it: unchanged, apart from being put on one line."""
    # A YAML folded scalar keeps the newlines around its more-indented lines, so a query
    # written across several lines for readability arrives here with them still in.
    topical = _WHITESPACE.sub(" ", topical).strip()
    if not topical:
        raise ValueError("the protocol's topical query is empty")
    return topical


def filter_for(protocol: Protocol) -> str:
    return search_query(protocol.query)
