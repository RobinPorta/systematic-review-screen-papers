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

from screener import endpoint  # noqa: E402
from screener.config import SetupError  # noqa: E402
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
    message = state.notice()
    if message:
        st.warning(message)
    with st.expander("🔒 Your session"):
        st.caption(
            "Your protocols and results are kept in server memory under the `ws` code in "
            "the address bar, never on disk. Reloading the page keeps them, and so does "
            "reopening the link — but only for an hour after your last action, and while the "
            "server is not restarted. Download what you want to keep. Anyone with the link "
            "can open this session, so share it only on purpose."
        )
        st.caption("Your API key is not kept: paste it again after a reload.")
        if st.button("Start a new, empty session", use_container_width=True):
            state.new_session()
            st.rerun()


def credentials_sidebar() -> None:
    """Per-visitor credentials. Kept in session state only — never written to disk."""
    env = state.load_settings()
    state.restore_endpoint_inputs()
    # Defaults go through session state: a widget given both a default and a restored
    # value makes Streamlit warn.
    st.session_state.setdefault(
        state.BACKEND, state.BACKEND_CUSTOM if env.custom_endpoint else state.BACKEND_TYPESAFE
    )
    st.session_state.setdefault(state.CONTEXT_LIMIT, env.context_limit)
    st.markdown("**Model endpoint**")
    backend = st.radio(
        "Model endpoint",
        [state.BACKEND_TYPESAFE, state.BACKEND_CUSTOM],
        key=state.BACKEND,
        label_visibility="collapsed",
        help="TypeSafe's hosted Jev, or your own server that speaks the same System One "
        "protocol, such as CLM (`clm-serve`).",
    )

    if backend == state.BACKEND_TYPESAFE:
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
    else:
        _custom_endpoint_inputs(env)
    state.remember_endpoint_inputs()

    settings = state.settings()
    if settings.custom_endpoint:
        st.caption(f"{'✓' if settings.endpoint_url else '✗'} Endpoint URL")
    else:
        st.caption(f"{'✓' if settings.typesafe_api_key else '✗'} TypeSafe key")
    st.caption("ERIC and Europe PMC need no key.")
    if settings.custom_endpoint and not settings.endpoint_url:
        st.warning("Enter your endpoint's URL above to run screening. Search works without it.")
    elif not settings.custom_endpoint and not settings.typesafe_api_key:
        st.warning("Paste a TypeSafe key above to run screening. Search works without one.")


def _custom_endpoint_inputs(env) -> None:
    st.text_input(
        "Endpoint URL",
        key=state.ENDPOINT_URL,
        placeholder=env.endpoint_url or "http://127.0.0.1:8700",
        help="The root of a server speaking the System One protocol — `clm-serve` "
        "listens on port 8700 by default. It must be reachable from where this app runs.",
    )
    st.text_input(
        "Endpoint API key",
        key=state.ENDPOINT_KEY,
        type="password",
        placeholder="set in environment" if env.endpoint_api_key else "optional",
        help="Only if the server requires one (`CLM_API_KEY`). Not kept after a reload.",
    )
    st.text_input(
        "Model",
        key=state.ENDPOINT_MODEL,
        placeholder=env.endpoint_model or "first model the server lists",
        help="Leave empty to use the first model at `/v1/models` (for CLM, `clm-latest`).",
    )
    st.number_input(
        "Context limit (tokens)",
        min_value=256,
        max_value=131_072,
        step=256,
        key=state.CONTEXT_LIMIT,
        help="The longest state the server accepts before truncating — `clm-serve "
        "--max-tokens`, 2048 by default. Stage 2 splits papers to stay under it.",
    )
    if st.button("Check connection", use_container_width=True):
        try:
            models = endpoint.list_models(state.settings())
        except SetupError as error:
            st.error(str(error))
        else:
            if models:
                st.success("Connected. Models: " + ", ".join(f"`{m}`" for m in models))
            else:
                st.warning("Connected, but the server lists no models.")


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
