"""Settings, with errors that say what to do about them.

The web app takes every value from its sidebar; the CLI reads them from environment
variables (`export TYPESAFE_API_KEY=...`). There is no `.env` file.

Screening runs against TypeSafe's hosted Jev by default, or against any self-hosted
server that speaks the same System One protocol — such as CLM (`clm-serve`) — when an
endpoint URL is set.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"

#: Jev allows 32k tokens for state plus the longest question. Stop well short: token counts
#: here are a four-characters-per-token estimate, not a real tokeniser.
JEV_STATE_BUDGET = 22_000
#: `clm-serve`'s default `--max-tokens`: longer states are truncated before embedding.
DEFAULT_CONTEXT_LIMIT = 2048
#: The same headroom idea as 22k against Jev's 32k, applied to a self-hosted limit.
_BUDGET_SHARE = 0.8
#: Local inference is slower than a hosted API; the SDK's 10-second default is too tight.
ENDPOINT_TIMEOUT = 120.0
#: The SDK refuses an empty key, but a server with no key configured accepts any.
_NO_KEY = "none"


class SetupError(RuntimeError):
    """Something about the configuration stops a run. The message says how to fix it."""


class MissingCredential(SetupError):
    """A required API key is not set. The message names the variable and where to get it."""


class InvalidEndpoint(SetupError):
    """The custom endpoint URL is not an http(s) URL."""


@dataclass(frozen=True)
class Settings:
    """Everything the pipeline needs from the environment.

    Only one of these is a credential. ERIC and Europe PMC need no key at all, which
    is most of the reason they are the sources here — a review that cannot be reproduced
    without an institutional subscription is a review most people cannot reproduce.
    """

    typesafe_api_key: str | None
    typesafe_model: str | None
    #: A self-hosted System One server, e.g. `http://127.0.0.1:8700` for `clm-serve`.
    #: None means TypeSafe's hosted API.
    endpoint_url: str | None = None
    #: Optional: only needed when the server was started with a key (`CLM_API_KEY`).
    endpoint_api_key: str | None = None
    #: None means the first model the server lists at `/v1/models`.
    endpoint_model: str | None = None
    #: The server's state limit in tokens. States longer than this are truncated there.
    context_limit: int = DEFAULT_CONTEXT_LIMIT

    @property
    def custom_endpoint(self) -> bool:
        return self.endpoint_url is not None

    @property
    def backend_label(self) -> str:
        return "Custom endpoint" if self.custom_endpoint else "Jev"

    @property
    def state_budget(self) -> int:
        """The largest stage-2 state to send, in estimated tokens."""
        if self.custom_endpoint:
            return max(256, int(self.context_limit * _BUDGET_SHARE))
        return JEV_STATE_BUDGET

    @property
    def cache_salt(self) -> str:
        """Keeps cached answers from one backend from being replayed as another's.

        Empty for TypeSafe, so answers cached before custom endpoints existed stay valid.
        """
        if not self.custom_endpoint:
            return ""
        return f"{(self.endpoint_url or '').rstrip('/')}|{self.endpoint_model or ''}"

    def client_options(self) -> dict[str, Any]:
        """Keyword arguments for the TypeSafe SDK client, for whichever backend is set."""
        if self.custom_endpoint:
            url = self.endpoint_url or ""
            if not url:
                raise InvalidEndpoint(
                    "No endpoint URL is set. Enter it in the sidebar (e.g. "
                    "http://127.0.0.1:8700 for clm-serve), or set SCREENER_ENDPOINT_URL."
                )
            if not url.startswith(("http://", "https://")):
                raise InvalidEndpoint(
                    f"The endpoint URL must start with http:// or https:// — got {url!r}."
                )
            return {
                "base_url": url.rstrip("/"),
                "api_key": self.endpoint_api_key or _NO_KEY,
                "model": self.endpoint_model,
                "timeout": ENDPOINT_TIMEOUT,
            }
        if not self.typesafe_api_key:
            raise MissingCredential(
                "TYPESAFE_API_KEY is not set. Create a key at "
                "https://console.typesafe.ai/keys and paste it in the sidebar of the "
                "web app, or export it for the CLI: export TYPESAFE_API_KEY=... "
                "Or point the app at a self-hosted endpoint instead. "
                "See the Credentials section of the README."
            )
        return {"api_key": self.typesafe_api_key, "model": self.typesafe_model}


def load_settings() -> Settings:
    def clean(name: str) -> str | None:
        value = os.environ.get(name, "").strip()
        return value or None

    limit = clean("SCREENER_CONTEXT_LIMIT")
    return Settings(
        typesafe_api_key=clean("TYPESAFE_API_KEY"),
        typesafe_model=clean("TYPESAFE_DEFAULT_MODEL"),
        endpoint_url=clean("SCREENER_ENDPOINT_URL"),
        endpoint_api_key=clean("SCREENER_ENDPOINT_API_KEY"),
        endpoint_model=clean("SCREENER_ENDPOINT_MODEL"),
        context_limit=int(limit) if limit and limit.isdigit() else DEFAULT_CONTEXT_LIMIT,
    )


def ensure_dirs() -> None:
    for directory in (
        DATA_DIR,
        CACHE_DIR,
        CACHE_DIR / "eric",
        CACHE_DIR / "jev",
        CACHE_DIR / "fulltext",
    ):
        directory.mkdir(parents=True, exist_ok=True)
