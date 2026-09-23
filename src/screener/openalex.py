"""OpenAlex search client.

OpenAlex needs no API key, which removes the single biggest source of friction in this
pipeline. Two things about it shape the code:

1. **Abstracts arrive as an inverted index**, `{word: [positions]}`, not as text. OpenAlex
   publishes them this way for copyright reasons; reconstructing the string is a few lines
   and gives back the original almost exactly.
2. **Rate limiting is credit-based.** Responses carry `X-RateLimit-*` headers including a
   remaining credit count and dollar allowance. Paging 200 at a time costs ~10 credits per
   page against a free daily allowance, which is ample for a review-sized corpus — but the
   client reads and reports the headers so a long run is never a mystery.

Supplying a contact email (`OPENALEX_MAILTO`) puts requests in OpenAlex's "polite pool",
which is faster and is simply good manners for a free service.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Iterator

import httpx

from .config import CACHE_DIR, Settings
from .records import Record, from_openalex_work

log = logging.getLogger(__name__)

WORKS_URL = "https://api.openalex.org/works"
#: OpenAlex's maximum page size.
MAX_PAGE_SIZE = 200


class OpenAlexError(RuntimeError):
    pass


class OpenAlexClient:
    """Cursor-paginating OpenAlex client with disk caching."""

    def __init__(
        self,
        settings: Settings,
        *,
        cache_dir: Path | None = None,
        timeout: float = 60.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._mailto = settings.openalex_mailto
        self._cache_dir = cache_dir if cache_dir is not None else CACHE_DIR / "openalex"
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=True)
        self._owns_client = client is None
        #: Populated from X-RateLimit-* headers so callers can report remaining allowance.
        self.credits_remaining: int | None = None
        self.usd_remaining: float | None = None

    def __enter__(self) -> OpenAlexClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _params(self, extra: dict[str, Any]) -> dict[str, Any]:
        params = dict(extra)
        if self._mailto:
            params["mailto"] = self._mailto
        return params

    def _get(self, params: dict[str, Any], *, attempts: int = 4) -> dict[str, Any]:
        delay = 2.0
        last: httpx.Response | None = None
        for attempt in range(attempts):
            response = self._client.get(
                WORKS_URL,
                params=self._params(params),
                headers={"User-Agent": f"sr-screener (mailto:{self._mailto or 'unset'})"},
            )
            last = response
            self._record_quota(response)

            if response.status_code == 200:
                return response.json()
            if response.status_code in (429, 500, 502, 503, 504) and attempt < attempts - 1:
                wait = float(response.headers.get("Retry-After") or delay)
                log.warning(
                    "OpenAlex returned %s, retrying in %.0fs (attempt %d/%d)",
                    response.status_code, wait, attempt + 1, attempts,
                )
                time.sleep(wait)
                delay *= 2
                continue
            break

        assert last is not None
        raise OpenAlexError(f"OpenAlex request failed ({last.status_code}): {_message(last)}")

    def _record_quota(self, response: httpx.Response) -> None:
        remaining = response.headers.get("X-RateLimit-Remaining")
        if remaining and remaining.lstrip("-").isdigit():
            self.credits_remaining = int(remaining)
        usd = response.headers.get("X-RateLimit-Remaining-USD")
        if usd:
            try:
                self.usd_remaining = float(usd)
            except ValueError:
                pass

    def count(self, filter_string: str) -> int:
        """How many works the filter matches, without paging through them."""
        payload = self._get({"filter": filter_string, "per-page": 1})
        return int(payload.get("meta", {}).get("count", 0))

    def search(
        self,
        filter_string: str,
        *,
        limit: int | None = None,
        page_size: int = MAX_PAGE_SIZE,
        use_cache: bool = True,
        on_page: Callable[[int, int], None] | None = None,
    ) -> Iterator[Record]:
        """Page through a filter with a cursor, yielding normalised records.

        Cursor paging rather than offsets: OpenAlex caps offset paging well below the size
        of a realistic review corpus, and a cursor is stable while you walk it.
        """
        page_size = max(1, min(page_size, MAX_PAGE_SIZE))
        cursor: str | None = "*"
        seen = 0
        page_index = 0

        while cursor:
            payload = self._cached_page(
                {"filter": filter_string, "per-page": page_size, "cursor": cursor},
                filter_string,
                page_index,
                use_cache=use_cache,
            )
            works = payload.get("results", []) or []
            if not works:
                return

            total = int(payload.get("meta", {}).get("count", 0))
            for work in works:
                yield from_openalex_work(work)
                seen += 1
                if limit is not None and seen >= limit:
                    return

            if on_page is not None:
                on_page(seen, total)

            next_cursor = payload.get("meta", {}).get("next_cursor")
            if not next_cursor or next_cursor == cursor:
                return
            cursor = next_cursor
            page_index += 1

    def _cached_page(
        self, params: dict[str, Any], filter_string: str, page_index: int, *, use_cache: bool
    ) -> dict[str, Any]:
        """Cache raw pages on disk, keyed by filter and page number.

        Re-running a screening pass after a code change should not re-fetch a corpus that
        has not moved — a courtesy to a free service as much as a saving.
        """
        if not use_cache:
            return self._get(params)

        key = f"{abs(hash(filter_string)):x}-{page_index:04d}.json"
        path = self._cache_dir / key
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))

        payload = self._get(params)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return payload


def _message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:300]
    return payload.get("message") or payload.get("error") or response.text[:300]
