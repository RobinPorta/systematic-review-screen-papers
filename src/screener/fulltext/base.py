"""What a full-text source has to provide.

Only Europe PMC is implemented, and it holds full text for its biomedical open-access
subset only — so on most corpora a large share of eligible records comes back `NOT_FOUND`.
That is not a bug to paper over: "full text not retrievable" is a box on the PRISMA flow
diagram, and this interface makes it a first-class outcome rather than an exception.

Adding another source — Unpaywall for open-access PDFs anywhere, a folder of PDFs your
library supplied — means implementing `FullTextSource` and adding it to the resolver chain.
"""

from __future__ import annotations

from enum import Enum
from typing import Protocol as TypingProtocol, Sequence, runtime_checkable

from pydantic import BaseModel, Field

from ..records import Record


class RetrievalStatus(str, Enum):
    OK = "ok"
    #: The source has the paper but this key is not entitled to its full text.
    NOT_ENTITLED = "not_entitled"
    #: The source does not carry this paper at all (e.g. outside the open-access subset).
    NOT_FOUND = "not_found"
    #: The record has no DOI, so it cannot be looked up.
    NO_IDENTIFIER = "no_identifier"
    #: Network or server failure. Worth retrying, unlike the others.
    ERROR = "error"


class FullText(BaseModel):
    """Retrieved full text, before section parsing."""

    record_id: str
    doi: str | None = None
    source: str
    #: Raw markup as fetched, kept so parsing can be re-run without re-fetching.
    content: str
    content_type: str = "xml"


class Retrieval(BaseModel):
    """The outcome of trying to obtain one paper."""

    record_id: str
    status: RetrievalStatus
    source: str | None = None
    full_text: FullText | None = None
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is RetrievalStatus.OK and self.full_text is not None


class CoverageReport(BaseModel):
    """How much of the shortlist is actually obtainable, counted before spending anything."""

    total: int = 0
    by_status: dict[str, int] = Field(default_factory=dict)

    @property
    def retrievable(self) -> int:
        return self.by_status.get(RetrievalStatus.OK.value, 0)

    @property
    def percentage(self) -> float:
        return 100.0 * self.retrievable / self.total if self.total else 0.0

    def add(self, status: RetrievalStatus) -> None:
        self.total += 1
        self.by_status[status.value] = self.by_status.get(status.value, 0) + 1


@runtime_checkable
class FullTextSource(TypingProtocol):
    """A place full texts can be obtained from."""

    name: str

    def check(self, record: Record) -> RetrievalStatus:
        """Can this source supply the paper? Cheap enough to run over a whole shortlist."""

    def fetch(self, record: Record) -> Retrieval:
        """Obtain the paper, or explain why not."""


def resolve(record: Record, sources: Sequence[FullTextSource]) -> Retrieval:
    """Try each source in turn; the first success wins.

    When every source declines, the most informative refusal is reported: being told
    "not entitled" is actionable (ask a librarian) in a way that "not found" is not.
    """
    attempts: list[Retrieval] = []
    for source in sources:
        retrieval = source.fetch(record)
        if retrieval.ok:
            return retrieval
        attempts.append(retrieval)

    if not attempts:
        return Retrieval(
            record_id=record.id,
            status=RetrievalStatus.ERROR,
            detail="no full-text sources are configured",
        )

    priority = [
        RetrievalStatus.NOT_ENTITLED,
        RetrievalStatus.ERROR,
        RetrievalStatus.NOT_FOUND,
        RetrievalStatus.NO_IDENTIFIER,
    ]
    for status in priority:
        for attempt in attempts:
            if attempt.status is status:
                return attempt
    return attempts[0]
