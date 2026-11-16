"""The pipeline steps, as plain functions.

Both the CLI and the Streamlit app call these. Keeping them here rather than in either
front end is what makes it possible to claim the UI is a *view* of the pipeline and not a
second implementation of it — and the verification step that runs both and compares the
numbers is only meaningful because of that.
"""

from __future__ import annotations

import asyncio
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Callable, Iterable, Iterator, Sequence

from pydantic import BaseModel, Field

from .config import Settings
from .decide import Label
from .eric import EricClient
from .fulltext import base as fulltext
from .fulltext.base import CoverageReport, FullText, FullTextSource, RetrievalStatus
from .fulltext.eric_pdf import EricPdfSource
from .fulltext.europepmc import EuropePMCSource
from .fulltext.sections import ParsedSections, parse_plain_text, parse_sections
from .protocol import Protocol
from .query import filter_for
from .records import FilterOutcome, Record, apply_filters, dedupe
from .screen import ScreenRun, screen_records
from .screen_fulltext import FullTextRun, screen_fulltexts

Progress = Callable[[int, int], None]


class SearchOutcome(BaseModel):
    query: str
    fetched: int = 0
    kept: list[Record] = Field(default_factory=list)
    dropped: dict[str, int] = Field(default_factory=dict)
    duplicates: int = 0
    without_abstract: int = 0

    @property
    def total(self) -> int:
        return len(self.kept)


def search(
    protocol: Protocol,
    *,
    settings: Settings,
    limit: int | None = None,
    use_cache: bool = True,
    on_page: Progress | None = None,
) -> SearchOutcome:
    """Run the protocol's query against ERIC and apply the code-side filters."""
    query = filter_for(protocol)
    with EricClient() as client:
        fetched = list(client.search(query, limit=limit, use_cache=use_cache, on_page=on_page))

    unique = dedupe(fetched)
    outcome: FilterOutcome = apply_filters(unique, protocol.filters)
    return SearchOutcome(
        query=query,
        fetched=len(fetched),
        kept=outcome.kept,
        dropped=outcome.drop_reasons,
        duplicates=len(fetched) - len(unique),
        # Reported, not dropped: no abstract is not evidence of ineligibility, so these go
        # forward and fail the `abstract_informative` guard into `maybe` for a human.
        without_abstract=sum(1 for record in outcome.kept if not record.has_abstract),
    )


def count_results(protocol: Protocol, *, settings: Settings) -> int:
    """How big is this search, before committing to it?"""
    with EricClient() as client:
        return client.count(filter_for(protocol))


def screen_abstracts(
    protocol: Protocol,
    records: Iterable[Record],
    *,
    settings: Settings,
    concurrency: int = 8,
    use_cache: bool = True,
    cache_dir: Path | None = None,
    on_progress: Progress | None = None,
) -> ScreenRun:
    return asyncio.run(
        screen_records(
            protocol,
            records,
            settings=settings,
            concurrency=concurrency,
            use_cache=use_cache,
            cache_dir=cache_dir,
            on_progress=on_progress,
        )
    )


def shortlist(run: ScreenRun) -> list[Record]:
    """What goes forward to full text.

    Both `include` and `maybe` advance. The screener's job is to decide what a human never
    has to read, not to settle eligibility on its own, so anything it was unsure about
    moves to the stage that can actually resolve it.
    """
    return [
        result.record
        for result in run.results
        if result.decision.label in (Label.INCLUDE, Label.MAYBE)
    ]


@contextmanager
def full_text_sources() -> Iterator[list[FullTextSource]]:
    """The sources stage 2 tries, in order: ERIC's own PDFs, then Europe PMC by DOI."""
    with ExitStack() as stack:
        yield [stack.enter_context(EricPdfSource()), stack.enter_context(EuropePMCSource())]


def parse_full_text(full_text: FullText, record: Record) -> ParsedSections:
    """Sections from whatever a source returned: structured XML, or text out of a PDF."""
    if full_text.content_type == "text":
        return parse_plain_text(full_text.content, title=record.title, abstract=record.abstract)
    return parse_sections(full_text.content)


def check_coverage(
    records: Sequence[Record],
    *,
    settings: Settings,
    on_progress: Progress | None = None,
) -> tuple[CoverageReport, dict[str, RetrievalStatus]]:
    """Ask the sources what they can actually supply, before fetching anything.

    Worth doing first every time: ERIC may display the full text of only some of its
    records, and Europe PMC holds its biomedical open-access subset only, so this number
    is the single most important fact about stage 2, and it costs nothing to learn.
    """
    report = CoverageReport()
    statuses: dict[str, RetrievalStatus] = {}
    with full_text_sources() as sources:
        for index, record in enumerate(records, start=1):
            status = fulltext.check(record, sources)
            statuses[record.id] = status
            report.add(status)
            if on_progress:
                on_progress(index, len(records))
    return report, statuses


def fetch_and_parse(
    records: Sequence[Record],
    *,
    settings: Settings,
    use_cache: bool = True,
    on_progress: Progress | None = None,
) -> list[tuple[Record, ParsedSections, RetrievalStatus]]:
    """Retrieve full texts and parse them into sections.

    Records that could not be retrieved stay in the list with an empty `ParsedSections` and
    their real status, so stage 2 can report them as PRISMA's "not retrieved" rather than
    letting them vanish between steps.
    """
    items: list[tuple[Record, ParsedSections, RetrievalStatus]] = []
    with full_text_sources() as sources:
        for index, record in enumerate(records, start=1):
            retrieval = fulltext.resolve(record, sources, use_cache=use_cache)
            if retrieval.ok and retrieval.full_text is not None:
                parsed = parse_full_text(retrieval.full_text, record)
                items.append((record, parsed, RetrievalStatus.OK))
            else:
                items.append((record, ParsedSections(), retrieval.status))
            if on_progress:
                on_progress(index, len(records))
    return items


def screen_full_texts(
    protocol: Protocol,
    items: Sequence[tuple[Record, ParsedSections, RetrievalStatus]],
    *,
    settings: Settings,
    concurrency: int = 4,
    use_cache: bool = True,
    label_unknown_sections: bool = True,
    cache_dir: Path | None = None,
    on_progress: Progress | None = None,
) -> FullTextRun:
    return asyncio.run(
        screen_fulltexts(
            protocol,
            items,
            settings=settings,
            concurrency=concurrency,
            use_cache=use_cache,
            cache_dir=cache_dir,
            label_unknown_sections=label_unknown_sections,
            on_progress=on_progress,
        )
    )


def load_protocol(path: str | Path) -> Protocol:
    return Protocol.load(path)
