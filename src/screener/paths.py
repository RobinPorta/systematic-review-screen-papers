"""Where the pipeline's artefacts live, so the CLI and the UI agree without arguing.

The CLI keeps one run directory per protocol under `data/runs/<protocol file stem>/`, so
two reviews never mix their records, results or labels. The web app only uses these in
single-user mode; otherwise it keeps everything in the browser session's memory.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import DATA_DIR, PROJECT_ROOT

#: Protocols that ship with the project. Read-only in the UI — duplicate one to edit it.
BUNDLED_PROTOCOLS_DIR = PROJECT_ROOT / "protocols"
#: Skeletons offered when creating a protocol. Not runnable protocols themselves.
TEMPLATES_DIR = BUNDLED_PROTOCOLS_DIR / "templates"
#: Protocols created from the UI in single-user mode, next to the CLI's runs.
USER_PROTOCOLS_DIR = DATA_DIR / "protocols"
RUNS_DIR = DATA_DIR / "runs"

DEFAULT_PROTOCOL = BUNDLED_PROTOCOLS_DIR / "boardgames.yaml"
STARTER_TEMPLATE = TEMPLATES_DIR / "starter.yaml"


@dataclass(frozen=True)
class RunPaths:
    """The files one protocol's run reads and writes."""

    root: Path

    @property
    def records(self) -> Path:
        return self.root / "records.jsonl"

    @property
    def screened(self) -> Path:
        return self.root / "screened.jsonl"

    @property
    def screened_csv(self) -> Path:
        return self.root / "screened.csv"

    @property
    def fulltext(self) -> Path:
        return self.root / "fulltext.jsonl"

    @property
    def fulltext_csv(self) -> Path:
        return self.root / "fulltext.csv"

    @property
    def labels_stage1(self) -> Path:
        return self.root / "labels_abstracts.csv"

    @property
    def labels_stage2(self) -> Path:
        return self.root / "labels_fulltext.csv"

    def ensure(self) -> RunPaths:
        self.root.mkdir(parents=True, exist_ok=True)
        return self


def run_paths(protocol_path: str | Path) -> RunPaths:
    """The run directory for a protocol, named after its file."""
    return RunPaths(RUNS_DIR / Path(protocol_path).stem).ensure()
