"""Session state: everything the UI shows lives in server memory, never on disk.

Protocols (the bundled examples included), fetched records, screening results and labels
are held in the in-memory `store`, keyed by the random `ws` code in the page URL. Reloading
the page, or reopening the link, finds them again. Once something is fetched or computed,
nothing on disk is consulted for it again, so deleting files on the server never touches a
session's work. A server restart, an hour of inactivity, or being the longest-idle of too
many sessions clears it: download what you want to keep.

The only files the app touches are the shared caches of public OpenAlex and Europe PMC
data, which are rebuilt on demand if they disappear.

Set `SCREENER_SINGLE_USER=1` to also load from, and write through to, `data/`: the same
files the CLI uses, so a run started in the browser can be finished in the terminal.
"""

from __future__ import annotations

import io
import os
import re
import secrets
from dataclasses import dataclass, field, replace

import pandas as pd
import streamlit as st

from .. import library, paths, report
from ..config import CACHE_DIR, Settings, ensure_dirs, load_settings
from ..protocol import Protocol
from ..records import Record, read_jsonl, write_jsonl
from ..screen import ScreenRun
from ..screen_fulltext import FullTextRun
from .store import SessionStore

#: Session-state keys of the credential inputs in the sidebar. They live only in this
#: browser session and are never written to disk, so each visitor brings their own key.
TYPESAFE_KEY = "typesafe_api_key"
TYPESAFE_MODEL = "typesafe_model"
OPENALEX_MAILTO = "openalex_mailto"

STAGES = ("abstracts", "fulltext")


def single_user() -> bool:
    return os.environ.get("SCREENER_SINGLE_USER", "").strip().lower() in {"1", "true", "yes"}


# ------------------------------------------------------------------ the session


WORKSPACE_PARAM = "ws"
#: What `secrets.token_urlsafe(18)` produces. Anything else in the URL is ignored.
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{24}$")


@st.cache_resource
def _store() -> SessionStore:
    """One store for the whole server process, shared by every browser connection."""
    return SessionStore()


def _token() -> str:
    token = st.session_state.get("ws_token")
    if token is None:
        from_url = st.query_params.get(WORKSPACE_PARAM, "")
        # Remember whether the code came from a link: if so, a missing session means it
        # was cleared, which is worth telling the visitor.
        st.session_state["ws_seen"] = bool(_TOKEN.match(from_url))
        token = from_url if _TOKEN.match(from_url) else secrets.token_urlsafe(18)
        st.session_state["ws_token"] = token
    if st.query_params.get(WORKSPACE_PARAM) != token:
        st.query_params[WORKSPACE_PARAM] = token
    return token


def _data() -> dict:
    """This session's data. Recreated empty, with a notice, if it expired or was dropped."""
    token = _token()
    data = _store().get(token)
    if data is None:
        if st.session_state.get("ws_seen"):
            st.session_state["notice"] = (
                "Your earlier session was cleared — an hour of inactivity, a server restart, "
                "or too many sessions at once — so this one starts fresh."
            )
        data = _store().create(token)
    st.session_state["ws_seen"] = True
    return data


def notice() -> str | None:
    """A one-off message about the session, shown once and then forgotten."""
    return st.session_state.pop("notice", None)


def new_session() -> None:
    """Start over with an empty session. The old one is dropped from memory."""
    _store().discard(_token())
    for key in ("ws_token", "ws_seen", "draft"):
        st.session_state.pop(key, None)
    st.query_params.pop(WORKSPACE_PARAM, None)


def _typed(key: str) -> str | None:
    value = (st.session_state.get(key) or "").strip()
    return value or None


def settings() -> Settings:
    """Environment settings, overridden by whatever was typed into the sidebar."""
    base = load_settings()
    return replace(
        base,
        typesafe_api_key=_typed(TYPESAFE_KEY) or base.typesafe_api_key,
        typesafe_model=_typed(TYPESAFE_MODEL) or base.typesafe_model,
        openalex_mailto=_typed(OPENALEX_MAILTO) or base.openalex_mailto,
    )


def jev_cache() -> dict:
    """Keyword arguments for the Jev cache.

    Jev answers are bought with the visitor's own key, so they are cached on disk only in
    single-user mode. Otherwise the results held in the session are the only copy.
    """
    if single_user():
        return {"use_cache": True, "cache_dir": CACHE_DIR / "jev"}
    return {"use_cache": False}


# ------------------------------------------------------------------ protocols


@dataclass
class Entry:
    id: str
    spec: Protocol
    bundled: bool

    @property
    def name(self) -> str:
        return self.spec.name


def _library() -> dict[str, Entry]:
    data = _data()
    if "library" not in data:
        entries = {pid: Entry(pid, spec, True) for pid, spec in library.bundled().items()}
        if single_user():
            for pid, spec in library.from_directory(paths.USER_PROTOCOLS_DIR).items():
                entries.setdefault(pid, Entry(pid, spec, False))
        data["library"] = entries
        data["starter"] = library.starter()
    return data["library"]


def entries() -> list[Entry]:
    """Bundled examples first, then the user's own, each alphabetically."""
    return sorted(_library().values(), key=lambda e: (not e.bundled, e.name.lower()))


def starter() -> Protocol:
    _library()
    return _data()["starter"]


def current_id() -> str:
    pid = _data().get("protocol_id", paths.DEFAULT_PROTOCOL.stem)
    return pid if pid in _library() else next(iter(_library()))


def current() -> Entry:
    return _library()[current_id()]


def protocol() -> Protocol:
    return current().spec


def is_editable() -> bool:
    """Bundled protocols are blueprints; only the user's own can be changed."""
    return not current().bundled


def select_protocol(pid: str) -> None:
    _data()["protocol_id"] = pid
    st.session_state.pop("draft", None)


def save_protocol(spec: Protocol) -> None:
    """Replace the current protocol's content. Only for the user's own protocols."""
    entry = current()
    if entry.bundled:
        raise ValueError("bundled protocols are read-only")
    entry.spec = spec
    st.session_state.pop("draft", None)
    if single_user():
        spec.save(paths.USER_PROTOCOLS_DIR / f"{entry.id}.yaml")


def create_protocol(spec: Protocol) -> str:
    """Add a new protocol to the session's library, select it and return its id."""
    taken = set(_library())
    if single_user() and paths.RUNS_DIR.exists():
        # A run directory left behind by a deleted protocol must not be inherited.
        taken |= {d.name for d in paths.RUNS_DIR.iterdir() if d.is_dir()}
    pid = library.unique_slug(spec.name, taken)
    _library()[pid] = Entry(pid, spec, False)
    if single_user():
        paths.USER_PROTOCOLS_DIR.mkdir(parents=True, exist_ok=True)
        spec.save(paths.USER_PROTOCOLS_DIR / f"{pid}.yaml")
    select_protocol(pid)
    return pid


def delete_protocol(pid: str) -> None:
    entry = _library().get(pid)
    if entry is None or entry.bundled:
        raise ValueError("only your own protocols can be deleted")
    del _library()[pid]
    _runs().pop(pid, None)
    if single_user():
        (paths.USER_PROTOCOLS_DIR / f"{pid}.yaml").unlink(missing_ok=True)
    select_protocol(paths.DEFAULT_PROTOCOL.stem)


# ------------------------------------------------------------------ run data


@dataclass
class Run:
    """Everything fetched or computed for one protocol in this session."""

    records: list[Record] = field(default_factory=list)
    screen: ScreenRun | None = None
    fulltext: FullTextRun | None = None
    #: stage -> the calibration sample, with its `gold_label` column.
    labels: dict[str, pd.DataFrame] = field(default_factory=dict)
    #: stage -> record ids skipped while labelling, sent to the back of the queue.
    skipped: dict[str, set[str]] = field(default_factory=dict)
    coverage: object | None = None
    result_count: int | None = None
    #: Rendered CSV downloads, built once per result rather than on every rerun.
    downloads: dict[str, str] = field(default_factory=dict)


def _runs() -> dict[str, Run]:
    return _data().setdefault("runs", {})


def _files() -> paths.RunPaths:
    return paths.run_paths(f"{current_id()}.yaml")


def _load_from_disk(data: Run) -> None:
    """Single-user mode only: pick up what the CLI (or an earlier session) left in data/."""
    files = _files()
    if files.records.exists():
        data.records = list(read_jsonl(files.records))
    if files.screened.exists():
        data.screen = ScreenRun.read_jsonl(files.screened)
    if files.fulltext.exists():
        data.fulltext = FullTextRun.read_jsonl(files.fulltext)
    for stage, path in zip(STAGES, (files.labels_stage1, files.labels_stage2)):
        if path.exists():
            data.labels[stage] = pd.read_csv(path, dtype=str).fillna("")


def run() -> Run:
    """The current protocol's session data, created on first use."""
    runs = _runs()
    pid = current_id()
    if pid not in runs:
        runs[pid] = Run()
        if single_user():
            _load_from_disk(runs[pid])
    return runs[pid]


def records() -> list[Record]:
    return run().records


def set_records(items: list[Record]) -> None:
    run().records = items
    if single_user():
        write_jsonl(items, _files().records)


def screen_run() -> ScreenRun | None:
    return run().screen


def set_screen_run(result: ScreenRun) -> None:
    data = run()
    data.screen = result
    data.downloads["screened"] = report.as_text(report.write_screening_csv, protocol(), result)
    if single_user():
        result.write_jsonl(_files().screened)
        _files().screened_csv.write_text(data.downloads["screened"], encoding="utf-8")


def fulltext_run() -> FullTextRun | None:
    return run().fulltext


def set_fulltext_run(result: FullTextRun) -> None:
    data = run()
    data.fulltext = result
    data.downloads["fulltext"] = report.as_text(report.write_fulltext_csv, protocol(), result)
    if single_user():
        result.write_jsonl(_files().fulltext)
        _files().fulltext_csv.write_text(data.downloads["fulltext"], encoding="utf-8")


def download(kind: str) -> str | None:
    """CSV text for `screened` or `fulltext`, rendered once and then kept."""
    data = run()
    if kind not in data.downloads:
        if kind == "screened" and data.screen:
            data.downloads[kind] = report.as_text(report.write_screening_csv, protocol(), data.screen)
        elif kind == "fulltext" and data.fulltext:
            data.downloads[kind] = report.as_text(report.write_fulltext_csv, protocol(), data.fulltext)
    return data.downloads.get(kind)


def labels(stage: str) -> pd.DataFrame | None:
    return run().labels.get(stage)


def draw_labels(stage: str, results: ScreenRun | FullTextRun, record_ids: list[str]) -> int:
    """Start a calibration sample for `stage`, replacing any previous one."""
    text = report.as_text(report.write_label_template, results, record_ids)
    frame = pd.read_csv(io.StringIO(text), dtype=str).fillna("")
    set_labels(stage, frame)
    run().skipped.pop(stage, None)
    return len(frame)


def set_labels(stage: str, frame: pd.DataFrame) -> None:
    run().labels[stage] = frame
    if single_user():
        files = _files()
        target = files.labels_stage1 if stage == "abstracts" else files.labels_stage2
        frame.to_csv(target, index=False)


def gold(stage: str) -> dict[str, str]:
    frame = labels(stage)
    return report.gold_labels(frame.to_dict("records")) if frame is not None else {}


def skipped(stage: str) -> set[str]:
    return run().skipped.setdefault(stage, set())


def bootstrap() -> None:
    ensure_dirs()
    _library()
    run()
