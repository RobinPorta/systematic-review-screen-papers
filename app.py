"""Streamlit front end.

    uv run streamlit run app.py

A view over `screener.pipeline`, which the CLI drives too — so anything shown here is
reproducible from the terminal, and a run started in the browser can be finished there.
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

# Make `src/` importable even when the project is not installed as a package.
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from screener.ui import editor, state, tabs  # noqa: E402

st.set_page_config(page_title="SR screener · Jev", page_icon="📚", layout="wide")


def protocol_sidebar() -> None:
    """Which review is being worked on. Each protocol has its own records and results."""
    entries = state.entries()
    ids = [e.id for e in entries]
    picked = st.selectbox(
        "Protocol",
        range(len(entries)),
        index=ids.index(state.current_id()),
        format_func=lambda i: entries[i].name + (" · example" if entries[i].bundled else ""),
        help="Create your own on the Protocol tab. Examples are read-only blueprints.",
    )
    if ids[picked] != state.current_id():
        state.select_protocol(ids[picked])
        st.rerun()

    if state.protocol().description:
        st.caption(state.protocol().description)


def session_sidebar() -> None:
    """Where this visitor's work is kept, which is: nowhere but this session."""
    if state.single_user():
        st.caption("Single-user mode: also reading and writing `data/`, shared with the CLI.")
        return
    st.caption(
        "🔒 Your protocols and results live only in this browser session, private to you. "
        "Reloading the page or a server restart clears them, so download what you want "
        "to keep."
    )


def credentials_sidebar() -> None:
    """Per-visitor credentials. Kept in session state only — never written to disk."""
    env = state.load_settings()
    st.markdown("**Credentials**")
    st.text_input(
        "TypeSafe API key",
        key=state.TYPESAFE_KEY,
        type="password",
        placeholder="set in environment" if env.typesafe_api_key else "ts_…",
        help="Create one at https://console.typesafe.ai/keys. Stays in this browser "
        "session only; it is not saved on the server.",
    )
    with st.expander("Optional"):
        st.text_input(
            "Jev model",
            key=state.TYPESAFE_MODEL,
            placeholder=env.typesafe_model or "jev-latest",
            help="Pin a model version instead of the `jev-latest` alias.",
        )
        st.text_input(
            "OpenAlex contact email",
            key=state.OPENALEX_MAILTO,
            placeholder=env.openalex_mailto or "you@example.org",
            help="Not authentication — puts OpenAlex requests in the faster polite pool.",
        )

    settings = state.settings()
    st.caption(f"{'✓' if settings.typesafe_api_key else '✗'} TypeSafe key")
    st.caption(f"{'·' if settings.openalex_mailto else '—'} OpenAlex contact (courtesy)")
    st.caption("OpenAlex and Europe PMC need no key.")
    if not settings.typesafe_api_key:
        st.warning("Paste a TypeSafe key above to run screening. Search works without one.")


def main() -> None:
    state.bootstrap()

    with st.sidebar:
        st.title("📚 SR screener")
        st.caption("Two-stage systematic review screening, built on TypeSafe's Jev.")

        protocol_sidebar()
        spec = state.protocol()
        session_sidebar()

        credentials_sidebar()

        st.markdown("**Thresholds**")
        st.caption(f"include ≥ {spec.thresholds.include_min:.2f}")
        st.caption(f"exclude ≥ {spec.thresholds.exclude_min:.2f}")
        st.caption("Asymmetric on purpose: screening is recall-critical.")

    protocol, search, screen, review, fulltext, label, evaluate = st.tabs(
        ["0 · Protocol", "1 · Search", "2 · Screen", "3 · Review", "4 · Full text",
         "5 · Label", "6 · Evaluate"]
    )
    with protocol:
        editor.protocol_tab()
    with search:
        tabs.search_tab()
    with screen:
        tabs.screen_tab()
    with review:
        tabs.review_tab()
    with fulltext:
        tabs.fulltext_tab()
    with label:
        tabs.label_tab()
    with evaluate:
        tabs.evaluate_tab()


if __name__ == "__main__":
    main()
