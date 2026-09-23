"""Protocol library helpers: the bundled examples, naming, and importing YAML.

Bundled protocols (`protocols/*.yaml`) are blueprints, read-only from the UI, so an
example stays an example. Where a user's own protocols are kept is up to the caller: the
web app keeps them in session memory, and writes them to `data/protocols/` as well only
in single-user mode.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

import yaml

from . import paths
from .protocol import Protocol


def slugify(text: str) -> str:
    """A file-name-safe version of a protocol name: `Sleep & Screens` -> `sleep-screens`."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return slug[:60].strip("-") or "protocol"


def unique_slug(name: str, taken: set[str]) -> str:
    """`slugify(name)`, suffixed until it clashes with nothing in `taken`.

    Protocol ids double as run-directory names, so a clash would make two protocols share
    — and overwrite — each other's results.
    """
    base = slugify(name)
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    return slug


def bundled() -> dict[str, Protocol]:
    """The example protocols that ship with the project, by id (file stem)."""
    return {path.stem: Protocol.load(path) for path in sorted(paths.BUNDLED_PROTOCOLS_DIR.glob("*.yaml"))}


def starter() -> Protocol:
    """The blank skeleton new protocols start from."""
    return Protocol.load(paths.STARTER_TEMPLATE)


def from_directory(directory: Path) -> dict[str, Protocol]:
    """Every valid protocol in `directory`, by id. Invalid files are skipped, not fatal."""
    found: dict[str, Protocol] = {}
    for path in sorted(directory.glob("*.yaml")) if directory.exists() else []:
        try:
            found[path.stem] = Protocol.load(path)
        except (OSError, ValueError, yaml.YAMLError):
            continue
    return found


def parse_yaml(text: str) -> Protocol:
    """Validate an uploaded protocol.

    Raises ValueError (pydantic's ValidationError included) when it is not a protocol.
    """
    raw = yaml.safe_load(text)
    if not isinstance(raw, dict):
        raise ValueError("the file does not contain a YAML mapping")
    return Protocol.model_validate(raw)


def to_yaml(protocol: Protocol) -> str:
    """The protocol as YAML, exactly as `Protocol.save` would write it."""
    payload = protocol.model_dump(mode="json", by_alias=True, exclude_none=True)
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, width=100)
