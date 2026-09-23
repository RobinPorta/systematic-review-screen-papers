"""Europe PMC full-text retrieval.

No key, no entitlement, and — the part that matters for stage 2 — it returns **JATS XML
with real `<sec>` structure**. That is why it is the full-text source here rather than a
PDF route: the section routing that keeps each criterion looking at the right two pages
depends on the paper arriving with its headings intact. Extracting text from a PDF would
flatten exactly the structure the design relies on.

The trade is coverage. Europe PMC is biomedical and holds full text only for its
open-access subset, so a good share of any corpus comes back `NOT_FOUND`. That is reported
as PRISMA's "reports not retrieved" rather than hidden.
"""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path

import httpx

from ..config import CACHE_DIR, Settings
from ..records import Record
from .base import FullText, Retrieval, RetrievalStatus

log = logging.getLogger(__name__)

BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"
SEARCH_URL = f"{BASE}/search"
FULLTEXT_URL = BASE + "/{pmcid}/fullTextXML"


class EuropePMCSource:
    """Resolves a record to a PMCID, then fetches its full-text XML."""

    name = "europepmc"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        cache_dir: Path | None = None,
        timeout: float = 90.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._cache_dir = cache_dir if cache_dir is not None else CACHE_DIR / "fulltext"
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=True)
        self._owns_client = client is None

    def __enter__(self) -> EuropePMCSource:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _cache_path(self, key: str) -> Path:
        digest = hashlib.sha256(key.lower().encode("utf-8")).hexdigest()[:24]
        return self._cache_dir / f"epmc-{digest}.xml"

    def _lookup(self, record: Record) -> tuple[str | None, str | None]:
        """Find the PMCID for a record, and say why if there is none.

        OpenAlex often carries the PMCID already, which saves a call. Otherwise the record
        is looked up by DOI, and the `inEPMC` flag tells us whether the full text is
        actually held here rather than merely indexed.
        """
        if record.pmcid:
            return record.pmcid, None
        if not record.doi:
            return None, "no DOI or PMCID to look up"

        try:
            response = self._client.get(
                SEARCH_URL,
                params={"query": f'DOI:"{record.doi}"', "format": "json", "resultType": "core"},
            )
        except httpx.HTTPError as error:
            return None, str(error)

        if response.status_code != 200:
            return None, f"lookup returned HTTP {response.status_code}"

        try:
            results = response.json().get("resultList", {}).get("result", [])
        except ValueError:
            return None, "lookup returned malformed JSON"

        if not results:
            return None, "not indexed in Europe PMC"

        hit = results[0]
        pmcid = hit.get("pmcid")
        if not pmcid:
            return None, "indexed in Europe PMC, but no PMC full text"
        if hit.get("inEPMC") != "Y" and hit.get("isOpenAccess") != "Y":
            return None, "full text is not in the Europe PMC open-access subset"
        return pmcid, None

    def check(self, record: Record) -> RetrievalStatus:
        """Can this source supply the paper? Cheap enough to run over a whole shortlist."""
        if record.pmcid and self._cache_path(record.pmcid).exists():
            return RetrievalStatus.OK
        pmcid, reason = self._lookup(record)
        if pmcid:
            return RetrievalStatus.OK
        if reason and "no DOI" in reason:
            return RetrievalStatus.NO_IDENTIFIER
        return RetrievalStatus.NOT_FOUND

    def fetch(self, record: Record, *, use_cache: bool = True, attempts: int = 3) -> Retrieval:
        pmcid, reason = self._lookup(record)
        if not pmcid:
            status = (
                RetrievalStatus.NO_IDENTIFIER
                if reason and "no DOI" in reason
                else RetrievalStatus.NOT_FOUND
            )
            return Retrieval(
                record_id=record.id, status=status, source=self.name, detail=reason
            )

        path = self._cache_path(pmcid)
        if use_cache and path.exists():
            return self._ok(record, pmcid, path.read_text(encoding="utf-8"))

        delay = 2.0
        for attempt in range(attempts):
            try:
                response = self._client.get(FULLTEXT_URL.format(pmcid=pmcid))
            except httpx.HTTPError as error:
                if attempt < attempts - 1:
                    time.sleep(delay)
                    delay *= 2
                    continue
                return Retrieval(
                    record_id=record.id, status=RetrievalStatus.ERROR,
                    source=self.name, detail=str(error),
                )

            if response.status_code == 200 and response.text.strip():
                if use_cache:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(response.text, encoding="utf-8")
                return self._ok(record, pmcid, response.text)
            if response.status_code == 404:
                return Retrieval(
                    record_id=record.id, status=RetrievalStatus.NOT_FOUND, source=self.name,
                    detail=f"{pmcid} has no full text in Europe PMC",
                )
            if response.status_code in (429, 500, 502, 503, 504) and attempt < attempts - 1:
                wait = float(response.headers.get("Retry-After") or delay)
                log.warning("Europe PMC %s for %s, retrying in %.0fs",
                            response.status_code, pmcid, wait)
                time.sleep(wait)
                delay *= 2
                continue

            return Retrieval(
                record_id=record.id, status=RetrievalStatus.ERROR,
                source=self.name, detail=f"HTTP {response.status_code}",
            )

        return Retrieval(
            record_id=record.id, status=RetrievalStatus.ERROR,
            source=self.name, detail="exhausted retries",
        )

    def _ok(self, record: Record, pmcid: str, content: str) -> Retrieval:
        return Retrieval(
            record_id=record.id,
            status=RetrievalStatus.OK,
            source=self.name,
            full_text=FullText(
                record_id=record.id, doi=record.doi, source=self.name, content=content
            ),
            detail=pmcid,
        )
