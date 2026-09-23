"""Settings, with errors that say what to do about them.

The web app takes every value from its sidebar; the CLI reads them from environment
variables (`export TYPESAFE_API_KEY=...`). There is no `.env` file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"


class MissingCredential(RuntimeError):
    """A required API key is not set. The message names the variable and where to get it."""


@dataclass(frozen=True)
class Settings:
    """Everything the pipeline needs from the environment.

    Only one of these is a credential. OpenAlex and Europe PMC need no key at all, which
    is most of the reason they are the sources here — a review that cannot be reproduced
    without an institutional subscription is a review most people cannot reproduce.
    """

    typesafe_api_key: str | None
    typesafe_model: str | None
    #: Contact address sent to OpenAlex. Not authentication — it puts requests in the
    #: "polite pool", which is faster, and is good manners towards a free service.
    openalex_mailto: str | None

    def require_typesafe(self) -> str:
        if not self.typesafe_api_key:
            raise MissingCredential(
                "TYPESAFE_API_KEY is not set. Create a key at "
                "https://console.typesafe.ai/keys and paste it in the sidebar of the "
                "web app, or export it for the CLI: export TYPESAFE_API_KEY=... "
                "See the Credentials section of the README."
            )
        return self.typesafe_api_key


def load_settings() -> Settings:
    def clean(name: str) -> str | None:
        value = os.environ.get(name, "").strip()
        return value or None

    return Settings(
        typesafe_api_key=clean("TYPESAFE_API_KEY"),
        typesafe_model=clean("TYPESAFE_DEFAULT_MODEL"),
        openalex_mailto=clean("OPENALEX_MAILTO"),
    )


def ensure_dirs() -> None:
    for directory in (
        DATA_DIR,
        CACHE_DIR,
        CACHE_DIR / "openalex",
        CACHE_DIR / "jev",
        CACHE_DIR / "fulltext",
    ):
        directory.mkdir(parents=True, exist_ok=True)
