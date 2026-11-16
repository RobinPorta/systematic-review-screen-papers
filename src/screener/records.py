"""The bibliographic record, and the state Jev sees.

Building the state is a design decision, not plumbing. Jev's jaggedness notes are explicit
that accuracy falls as the state grows with content unrelated to the decision, so this
module is deliberately stingy: title, abstract, descriptors, journal, publication type and
education level. No authors, no citation, no identifiers — none of which bear on
eligibility.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any, Iterable, Iterator

from pydantic import BaseModel, Field

from .protocol import Filters
from .query import language_names


class Record(BaseModel):
    """One search result, normalised out of an ERIC record."""

    #: ERIC number: `EJ…` for a journal article, `ED…` for any other document.
    id: str
    #: Taken from ERIC's publisher link when that link is a DOI. Stage 2's route to
    #: Europe PMC for papers ERIC holds no PDF of.
    doi: str | None = None
    title: str = ""
    abstract: str = ""
    #: ERIC descriptors: controlled-vocabulary subject terms assigned to every record.
    keywords: list[str] = Field(default_factory=list)
    #: The journal, or for non-journal documents the organisation that submitted it.
    journal: str = ""
    year: int | None = None
    #: ERIC publication types, up to three, e.g. "Journal Articles", "Reports - Research".
    publication_types: list[str] = Field(default_factory=list)
    #: Language names as ERIC records them. Empty means English, ERIC's default.
    languages: list[str] = Field(default_factory=list)
    #: Every author, "Lastname, Firstname", separated by "; ".
    authors: str = ""
    url: str | None = None
    education_levels: list[str] = Field(default_factory=list)
    peer_reviewed: bool | None = None
    #: Volume, issue and pages for an article; conference or thesis details otherwise.
    citation: str = ""
    #: ERIC may display the full text, as a PDF at files.eric.ed.gov. Stage 2's first route.
    eric_fulltext: bool = False
    pmcid: str | None = None

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
        if self.publication_types:
            state["publication_type"] = self.publication_types
        if self.education_levels:
            state["education_level"] = self.education_levels
        return state


#: How many descriptors go into the state. ERIC assigns a dozen or more; the first ones
#: are the major descriptors, and a long tail adds tokens without adding judgement.
MAX_KEYWORDS = 12

_ESCAPED_COMMA = re.compile(r"(?<!\\),")
_DOI = re.compile(r"(?:dx\.)?doi\.org/(10\.\S+)", re.IGNORECASE)


def split_multi(value: str | None) -> list[str]:
    """Split one of ERIC's multi-valued CSV cells.

    Values are joined with commas, and a comma that belongs to a value is written `\\,` —
    which is how `Poston\\, David I.,Vandenkieboom\\, Kathryn K.` stays two authors.
    """
    if not value or not value.strip():
        return []
    parts = (part.replace("\\,", ",").strip() for part in _ESCAPED_COMMA.split(value))
    return [part for part in parts if part]


def eric_url(eric_id: str) -> str:
    return f"https://eric.ed.gov/?id={eric_id}"


def from_eric_row(row: dict[str, str]) -> Record:
    """Normalise one row of ERIC's CSV.

    Written defensively: ERIC leaves cells empty freely, and a record missing its source
    or its authors is still perfectly screenable.
    """
    def cell(name: str) -> str:
        # ERIC HTML-escapes its text: "Dungeons &amp; Dragons".
        return html.unescape(row.get(name) or "").strip()

    eric_id = cell("id") or "unknown"
    link = cell("url")
    doi = _DOI.search(link)
    year = cell("publicationdateyear")
    reviewed = cell("peerreviewed").upper()

    return Record(
        id=eric_id,
        doi=doi.group(1).rstrip(".") if doi else None,
        title=cell("title"),
        abstract=cell("description"),
        keywords=split_multi(cell("subject"))[:MAX_KEYWORDS],
        journal=cell("source"),
        year=int(year) if year.isdigit() else None,
        publication_types=split_multi(cell("publicationtype")),
        languages=split_multi(cell("language")),
        authors="; ".join(split_multi(cell("author"))),
        url=link or eric_url(eric_id),
        education_levels=split_multi(cell("educationlevel")),
        peer_reviewed={"T": True, "F": False}.get(reviewed),
        citation=cell("sourceid"),
        eric_fulltext=cell("e_fulltextauth") == "1",
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
    """Apply the protocol's year, language, publication type and peer-review filters.

    The query goes to ERIC exactly as written, so this is where the filters take effect.
    Every field checked here comes back on the record itself, and each dropped record is
    reported with its reason.

    A record with no abstract is dropped only when the protocol asks for that. Absence of
    an abstract is not evidence of ineligibility; left in, such records fail the
    `abstract_informative` guard during screening and land in `maybe` for a human, which
    is what PRISMA expects.
    """
    kept: list[Record] = []
    dropped: list[tuple[str, str]] = []

    allowed_types = {value.strip().lower() for value in filters.types if value.strip()}
    allowed_languages = {name.lower() for name in language_names(filters.languages)}

    for record in records:
        if filters.year_from is not None and (record.year is None or record.year < filters.year_from):
            dropped.append((record.id, f"published before {filters.year_from}"))
            continue
        if filters.year_to is not None and (record.year is None or record.year > filters.year_to):
            dropped.append((record.id, f"published after {filters.year_to}"))
            continue
        types = {value.lower() for value in record.publication_types}
        if allowed_types and not types & allowed_types:
            dropped.append((record.id, "publication type not among those the protocol keeps"))
            continue
        # A record with no language is kept: ERIC's default is English, and dropping on a
        # metadata gap would lose eligible studies.
        languages = {value.lower() for value in record.languages}
        if allowed_languages and languages and not languages & allowed_languages:
            dropped.append((record.id, f"language {', '.join(record.languages)}"))
            continue
        if filters.peer_reviewed_only and record.peer_reviewed is not True:
            dropped.append((record.id, "not peer reviewed"))
            continue
        if filters.require_abstract and not record.has_abstract:
            dropped.append((record.id, "no abstract"))
            continue
        kept.append(record)

    return FilterOutcome(kept=kept, dropped=dropped)


def dedupe(records: Iterable[Record]) -> list[Record]:
    """Drop repeats by ERIC number, then by DOI, then by normalised title."""
    seen_id: set[str] = set()
    seen_doi: set[str] = set()
    seen_title: set[str] = set()
    unique: list[Record] = []
    for record in records:
        doi = (record.doi or "").lower().strip()
        title = re.sub(r"\W+", " ", record.title.lower()).strip()
        if record.id in seen_id or (doi and doi in seen_doi):
            continue
        seen_id.add(record.id)
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
