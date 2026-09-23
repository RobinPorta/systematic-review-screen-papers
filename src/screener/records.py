"""The bibliographic record, and the state Jev sees.

Building the state is a design decision, not plumbing. Jev's jaggedness notes are explicit
that accuracy falls as the state grows with content unrelated to the decision, so this
module is deliberately stingy: title, abstract, keywords, journal, publication type. No
authors, no citation counts, no identifiers — none of which bear on eligibility.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Iterator

from pydantic import BaseModel, Field

from .protocol import Filters

_YEAR = re.compile(r"(\d{4})")


class Record(BaseModel):
    """One search result, normalised out of an OpenAlex work."""

    id: str
    doi: str | None = None
    title: str = ""
    abstract: str = ""
    keywords: list[str] = Field(default_factory=list)
    journal: str = ""
    year: int | None = None
    cover_date: str | None = None
    #: OpenAlex work type, e.g. "article", "review", "conference-paper".
    work_type: str | None = None
    #: ISO 639-1 code as OpenAlex reports it.
    language: str | None = None
    authors: str = ""
    url: str | None = None
    #: PubMed Central id, when OpenAlex knows one. Stage 2's route to full text.
    pmcid: str | None = None
    pmid: str | None = None
    is_oa: bool = False

    @property
    def has_abstract(self) -> bool:
        return bool(self.abstract.strip())

    def state(self) -> dict[str, Any]:
        """The stage-1 state: what Jev is asked to judge, and nothing else.

        Year is deliberately absent. It is already enforced by the query and by
        `apply_filters`, and Jev is documented as unreliable at comparing dates — putting
        it here could only invite a question that should never be asked.
        """
        state: dict[str, Any] = {"title": self.title, "abstract": self.abstract}
        if self.keywords:
            state["keywords"] = self.keywords
        if self.journal:
            state["journal"] = self.journal
        if self.work_type:
            state["publication_type"] = self.work_type
        return state


def parse_year(cover_date: str | None) -> int | None:
    if not cover_date:
        return None
    match = _YEAR.search(cover_date)
    return int(match.group(1)) if match else None


def abstract_from_inverted_index(index: dict[str, list[int]] | None) -> str:
    """Rebuild abstract text from OpenAlex's `{word: [positions]}` form.

    OpenAlex publishes abstracts inverted rather than as running text for copyright
    reasons. Reversing it recovers the original almost exactly — word order is fully
    determined by the positions; only the original whitespace and some punctuation
    spacing are lost, neither of which matters to a screening judgement.
    """
    if not index:
        return ""
    positions: dict[int, str] = {}
    for word, indices in index.items():
        for position in indices:
            positions[position] = word
    if not positions:
        return ""
    return " ".join(positions[key] for key in sorted(positions))


def _strip_prefix(value: str | None, prefix: str) -> str | None:
    """OpenAlex returns identifiers as full URLs; the bare id is what is useful."""
    if not value:
        return None
    return value.rsplit("/", 1)[-1] if value.startswith(prefix) else value


def from_openalex_work(work: dict[str, Any]) -> Record:
    """Normalise one OpenAlex work.

    Written defensively throughout: OpenAlex omits or nulls fields freely, and a record
    missing its venue or its authors is still perfectly screenable.
    """
    identifiers = work.get("ids") or {}
    primary = work.get("primary_location") or {}
    source = primary.get("source") or {}
    open_access = work.get("open_access") or {}

    doi = work.get("doi")
    if isinstance(doi, str) and doi.startswith("https://doi.org/"):
        doi = doi[len("https://doi.org/"):]

    authorships = work.get("authorships") or []
    first_author = ""
    if authorships:
        first_author = ((authorships[0] or {}).get("author") or {}).get("display_name") or ""

    date = work.get("publication_date")
    openalex_id = _strip_prefix(identifiers.get("openalex") or work.get("id"), "https://") or ""

    return Record(
        id=openalex_id or doi or "unknown",
        doi=doi or None,
        title=(work.get("display_name") or work.get("title") or "").strip(),
        abstract=abstract_from_inverted_index(work.get("abstract_inverted_index")),
        keywords=[
            k.get("display_name", "")
            for k in (work.get("keywords") or [])
            if k.get("display_name")
        ][:8],
        journal=source.get("display_name") or "",
        year=work.get("publication_year") or parse_year(date),
        cover_date=date,
        work_type=work.get("type"),
        language=work.get("language"),
        authors=first_author,
        url=primary.get("landing_page_url") or identifiers.get("openalex"),
        pmcid=_strip_prefix(identifiers.get("pmcid"), "https://"),
        pmid=_strip_prefix(identifiers.get("pmid"), "https://"),
        is_oa=bool(open_access.get("is_oa")),
    )


class FilterOutcome(BaseModel):
    """Which records survived the code-side filters, and why the others did not."""

    kept: list[Record]
    dropped: list[tuple[str, str]] = Field(default_factory=list)

    @property
    def drop_reasons(self) -> dict[str, int]:
        tally: dict[str, int] = {}
        for _, reason in self.dropped:
            tally[reason] = tally.get(reason, 0) + 1
        return tally


def apply_filters(records: Iterable[Record], filters: Filters) -> FilterOutcome:
    """Re-check year, language and work type locally.

    The OpenAlex query already filtered on all three, so this is a belt-and-braces pass
    that catches a malformed filter string before it quietly changes a review's corpus.
    Every field checked here comes back on the work itself, so unlike a Scopus
    `LANGUAGE()` clause these really are verifiable rather than taken on trust.

    A record with no abstract is deliberately **not** dropped here. Absence of an abstract
    is not evidence of ineligibility; such records fail the `abstract_informative` guard
    during screening and land in `maybe` for a human, which is what PRISMA expects.
    """
    kept: list[Record] = []
    dropped: list[tuple[str, str]] = []

    allowed_types = {value.strip().lower() for value in filters.types if value.strip()}
    allowed_languages = {value.strip().lower() for value in filters.languages if value.strip()}

    for record in records:
        if filters.year_from is not None and (record.year is None or record.year < filters.year_from):
            dropped.append((record.id, f"published before {filters.year_from}"))
            continue
        if filters.year_to is not None and (record.year is None or record.year > filters.year_to):
            dropped.append((record.id, f"published after {filters.year_to}"))
            continue
        if allowed_types and (record.work_type or "").lower() not in allowed_types:
            dropped.append((record.id, f"work type {record.work_type or 'unknown'}"))
            continue
        # A work with no language recorded is kept: OpenAlex leaves the field null often
        # enough that dropping on it would lose eligible studies for a metadata gap.
        if allowed_languages and record.language and record.language.lower() not in allowed_languages:
            dropped.append((record.id, f"language {record.language}"))
            continue
        kept.append(record)

    return FilterOutcome(kept=kept, dropped=dropped)


def dedupe(records: Iterable[Record]) -> list[Record]:
    """Drop repeats by DOI, then by normalised title."""
    seen_doi: set[str] = set()
    seen_title: set[str] = set()
    unique: list[Record] = []
    for record in records:
        doi = (record.doi or "").lower().strip()
        title = re.sub(r"\W+", " ", record.title.lower()).strip()
        if doi and doi in seen_doi:
            continue
        if not doi and title and title in seen_title:
            continue
        if doi:
            seen_doi.add(doi)
        if title:
            seen_title.add(title)
        unique.append(record)
    return unique


def write_jsonl(records: Iterable[Record], path: str | Path) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(record.model_dump_json() + "\n")
            count += 1
    return count


def read_jsonl(path: str | Path) -> Iterator[Record]:
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield Record.model_validate(json.loads(line))
