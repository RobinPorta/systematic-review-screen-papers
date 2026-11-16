"""ERIC search client.

ERIC (the U.S. Institute of Education Sciences' index of education research) needs no API
key. Three things about its API shape the code:

1. **Records are fetched as CSV**, paged with `start` and `rows`. Multi-valued cells
   (authors, descriptors, publication types) are comma-joined with literal commas escaped
   as `\\,`; `records.split_multi` undoes that.
2. **CSV reports neither the total nor errors.** A malformed query comes back as a bare
   header row, indistinguishable from "no results". So every search starts with one tiny
   JSON request (`rows=0`), which returns the total and, for a bad query, Solr's own
   error message.
3. **Errors arrive with HTTP 200.** The status code alone says nothing; the JSON body has
   an `error` object instead of a `response`.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import time
from pathlib import Path
from typing import Any, Callable, Iterator

import httpx

from .config import CACHE_DIR
from .records import Record, from_eric_row

log = logging.getLogger(__name__)

SEARCH_URL = "https://api.ies.ed.gov/eric/"
#: ERIC allows up to 2000 rows a request. Smaller pages keep each response a few hundred
#: kilobytes and make progress visible.
PAGE_SIZE = 500
MAX_PAGE_SIZE = 2000

#: What is asked of ERIC for every record. The first six are the bibliographic minimum;
#: the rest are what screening needs: the abstract (`description`), the descriptors, the
#: publication type and education level that go into the state, the peer-review flag for
#: filtering, the citation, and whether ERIC can serve the full-text PDF for stage 2.
FIELDS = (
    "id", "title", "author", "publicationdateyear", "language", "url",
    "description", "subject", "publicationtype", "source", "sourceid",
    "peerreviewed", "educationlevel", "e_fulltextauth", "issn",
)


class EricError(RuntimeError):
    pass


class EricClient:
    """Offset-paginating ERIC client with disk caching."""

    def __init__(
        self,
        *,
        cache_dir: Path | None = None,
        timeout: float = 60.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._cache_dir = cache_dir if cache_dir is not None else CACHE_DIR / "eric"
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=True)
        self._owns_client = client is None

    def __enter__(self) -> EricClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _get(self, params: dict[str, Any], *, attempts: int = 4) -> httpx.Response:
        delay = 2.0
        last: httpx.Response | None = None
        for attempt in range(attempts):
            try:
                response = self._client.get(
                    SEARCH_URL, params=params, headers={"User-Agent": "sr-screener"}
                )
            except httpx.HTTPError as error:
                raise EricError(f"Could not reach ERIC: {error}") from error
            last = response

            if response.status_code == 200:
                return response
            if response.status_code == 414:
                raise EricError(
                    "The query is too long for ERIC to accept (HTTP 414). Shorten it — for "
                    "example by dropping variants ERIC already matches — or split the "
                    "search in two."
                )
            if response.status_code in (429, 500, 502, 503, 504) and attempt < attempts - 1:
                wait = float(response.headers.get("Retry-After") or delay)
                log.warning(
                    "ERIC returned %s, retrying in %.0fs (attempt %d/%d)",
                    response.status_code, wait, attempt + 1, attempts,
                )
                time.sleep(wait)
                delay *= 2
                continue
            break

        assert last is not None
        raise EricError(f"ERIC request failed (HTTP {last.status_code}): {last.text[:300]}")

    def count(self, query: str) -> int:
        """How many records the query matches. Raises EricError if ERIC cannot parse it."""
        response = self._get({"search": query, "format": "json", "rows": 0})
        try:
            payload = response.json()
        except ValueError as error:
            raise EricError("ERIC returned something that is not JSON.") from error
        if "error" in payload:
            raise EricError(f"ERIC could not run the query: {_solr_message(payload['error'])}")
        return int(payload.get("response", {}).get("numFound", 0))

    def search(
        self,
        query: str,
        *,
        limit: int | None = None,
        page_size: int = PAGE_SIZE,
        use_cache: bool = True,
        on_page: Callable[[int, int], None] | None = None,
    ) -> Iterator[Record]:
        """Page through a query, yielding normalised records.

        The count comes first: it is what turns a malformed query into an error instead of
        an empty result, and it gives the progress bar its total.
        """
        total = self.count(query)
        page_size = max(1, min(page_size, MAX_PAGE_SIZE))
        wanted = total if limit is None else min(total, limit)
        seen = 0

        while seen < wanted:
            text = self._cached_page(query, start=seen, rows=page_size, use_cache=use_cache)
            rows = list(csv.DictReader(io.StringIO(text, newline="")))
            if not rows:
                return
            for row in rows:
                yield from_eric_row(row)
                seen += 1
                if seen >= wanted:
                    break
            if on_page is not None:
                on_page(seen, wanted)
            if len(rows) < page_size:
                return

    def _cached_page(self, query: str, *, start: int, rows: int, use_cache: bool) -> str:
        """Cache raw CSV pages on disk, keyed by query, fields and offset.

        Re-running a screening pass after a code change should not re-fetch a corpus that
        has not moved — a courtesy to a free service as much as a saving.
        """
        params = {
            "search": query, "format": "csv", "start": start, "rows": rows,
            "fields": ",".join(FIELDS),
        }
        if not use_cache:
            return self._get(params).text

        key = hashlib.sha256(
            "|".join([query, ",".join(FIELDS), str(start), str(rows)]).encode("utf-8")
        ).hexdigest()[:32]
        path = self._cache_dir / f"{key}.csv"
        if path.exists():
            return path.read_text(encoding="utf-8")

        text = self._get(params).text
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return text


def _solr_message(error: Any) -> str:
    """The readable part of a Solr error: the parser's complaint, not the Java class."""
    message = str(error.get("msg") if isinstance(error, dict) else error)
    return message.replace("org.apache.solr.search.SyntaxError: ", "").strip()[:300]
