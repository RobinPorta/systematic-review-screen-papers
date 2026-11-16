"""Full text from ERIC itself, for the records it is allowed to display.

ERIC flags such records with `e_fulltextauth = 1` and serves them as PDFs at
`files.eric.ed.gov/fulltext/<ERIC number>.pdf`. That flag comes back with the search, so
knowing whether a paper is obtainable costs no request at all.

A PDF is a much poorer source than Europe PMC's XML: there is no markup, so sections have
to be recognised from the text (`sections.parse_plain_text`), and an old scanned report may
carry no text layer. Only the extracted text is cached — a fraction of the PDF's size.
"""

from __future__ import annotations

import io
import logging
import re
from pathlib import Path

import httpx

from ..config import CACHE_DIR
from ..records import Record
from .base import FullText, Retrieval, RetrievalStatus

log = logging.getLogger(__name__)

PDF_URL = "https://files.eric.ed.gov/fulltext/{eric_id}.pdf"
#: Refuse anything larger: a screening tool has no business pulling a 200 MB scan into
#: memory, and nothing that size is a journal article.
MAX_PDF_BYTES = 25 * 1024 * 1024
MAX_PAGES = 150
#: Below this, the "text" is page furniture from a scan with no real text layer.
MIN_TEXT_CHARS = 500

_ERIC_ID = re.compile(r"^E[DJ]\d+$")


def extract_text(pdf: bytes) -> str:
    """The text of a PDF, page by page. Empty when it cannot be read."""
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    # pypdf reports every font oddity as a warning; none of them is actionable here.
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    try:
        reader = PdfReader(io.BytesIO(pdf))
        pages = [page.extract_text() or "" for page in reader.pages[:MAX_PAGES]]
    except (PyPdfError, ValueError, KeyError, OSError, RecursionError) as error:
        log.warning("could not read a PDF: %s", error)
        return ""
    return "\n".join(pages)


class EricPdfSource:
    """Fetches the PDF ERIC holds for a record and extracts its text."""

    name = "eric"

    def __init__(
        self,
        *,
        cache_dir: Path | None = None,
        timeout: float = 90.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._cache_dir = cache_dir if cache_dir is not None else CACHE_DIR / "fulltext"
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=True)
        self._owns_client = client is None

    def __enter__(self) -> EricPdfSource:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _cache_path(self, eric_id: str) -> Path:
        return self._cache_dir / f"eric-{eric_id}.txt"

    def check(self, record: Record) -> RetrievalStatus:
        """Whether ERIC can supply the paper, read off the record: no request needed."""
        if not _ERIC_ID.match(record.id):
            return RetrievalStatus.NO_IDENTIFIER
        return RetrievalStatus.OK if record.eric_fulltext else RetrievalStatus.NOT_FOUND

    def fetch(self, record: Record, *, use_cache: bool = True) -> Retrieval:
        status = self.check(record)
        if status is not RetrievalStatus.OK:
            detail = (
                "not an ERIC record" if status is RetrievalStatus.NO_IDENTIFIER
                else "ERIC holds no full-text PDF for this record"
            )
            return self._failed(record, status, detail)

        path = self._cache_path(record.id)
        if use_cache and path.exists():
            return self._ok(record, path.read_text(encoding="utf-8"))

        try:
            pdf = self._download(record.id)
        except httpx.HTTPError as error:
            return self._failed(record, RetrievalStatus.ERROR, str(error))
        if isinstance(pdf, Retrieval):
            return pdf.model_copy(update={"record_id": record.id})

        text = extract_text(pdf)
        if len(text.strip()) < MIN_TEXT_CHARS:
            return self._failed(
                record, RetrievalStatus.NOT_FOUND,
                "the PDF has no extractable text (probably a scan without a text layer)",
            )
        if use_cache:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return self._ok(record, text)

    def _download(self, eric_id: str) -> bytes | Retrieval:
        """The PDF's bytes, or the reason there are none."""
        with self._client.stream("GET", PDF_URL.format(eric_id=eric_id)) as response:
            if response.status_code == 404:
                return Retrieval(
                    record_id=eric_id, status=RetrievalStatus.NOT_FOUND, source=self.name,
                    detail="ERIC lists a full text, but the PDF is not there",
                )
            if response.status_code != 200:
                return Retrieval(
                    record_id=eric_id, status=RetrievalStatus.ERROR, source=self.name,
                    detail=f"HTTP {response.status_code}",
                )
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > MAX_PDF_BYTES:
                    return Retrieval(
                        record_id=eric_id, status=RetrievalStatus.NOT_FOUND, source=self.name,
                        detail=f"the PDF is larger than {MAX_PDF_BYTES // (1024 * 1024)} MB",
                    )
                chunks.append(chunk)
            return b"".join(chunks)

    def _failed(self, record: Record, status: RetrievalStatus, detail: str) -> Retrieval:
        return Retrieval(record_id=record.id, status=status, source=self.name, detail=detail)

    def _ok(self, record: Record, text: str) -> Retrieval:
        return Retrieval(
            record_id=record.id,
            status=RetrievalStatus.OK,
            source=self.name,
            full_text=FullText(
                record_id=record.id, doi=record.doi, source=self.name,
                content=text, content_type="text",
            ),
            detail=PDF_URL.format(eric_id=record.id),
        )
