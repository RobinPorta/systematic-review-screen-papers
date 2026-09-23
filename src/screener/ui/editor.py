"""The Protocol tab: pick, create, import and edit review protocols.

Editing works on a plain-dict *draft* held in session state. Widgets read from and write
back to it on every rerun, and nothing touches disk until Save validates the whole draft
as a `Protocol` — so a half-written criterion can never break the other tabs.

Outcome descriptions and Score levels in YAML are either a plain string or a
`{what, examples}` mapping. The form edits exactly those two shapes; anything more
structured is left untouched and can be changed in the raw YAML editor.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st
import yaml
from pydantic import ValidationError

from .. import library
from ..protocol import Protocol, Section
from . import state

KINDS = ["inclusion", "exclusion", "guard"]
SECTIONS = [s.value for s in Section]
LANGUAGES = ["en", "it", "fr", "de", "es", "pt", "nl"]
WORK_TYPES = ["article", "review", "conference-paper", "book-chapter", "preprint", "dissertation"]


# ------------------------------------------------------------------ pure helpers


def split_described(value: Any) -> tuple[str, list[str]] | None:
    """`"text"` or `{what, examples}` -> (what, examples). None if it is any other shape."""
    if value is None:
        return "", []
    if isinstance(value, str):
        return value, []
    if (
        isinstance(value, dict)
        and set(value) <= {"what", "examples"}
        and isinstance(value.get("what", ""), str)
        and all(isinstance(e, str) for e in value.get("examples") or [])
    ):
        return value.get("what", ""), list(value.get("examples") or [])
    return None


def join_described(what: str, examples: list[str]) -> Any:
    """The inverse of `split_described`, choosing the simplest shape that holds the data."""
    what = what.strip()
    examples = [e.strip() for e in examples if e.strip()]
    if examples:
        return {"what": what, "examples": examples}
    return what or None


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def validation_messages(error: ValidationError | ValueError) -> list[str]:
    """Pydantic errors as one readable line each, e.g. `criteria.2.label: Field required`."""
    if isinstance(error, ValidationError):
        return [
            f"{'.'.join(str(p) for p in e['loc']) or 'protocol'}: {e['msg']}"
            for e in error.errors()
        ]
    return [str(error)]


# ------------------------------------------------------------------ draft state


def _draft() -> dict:
    """The protocol being edited, as a dict. Loaded lazily from the current protocol."""
    if "draft" not in st.session_state:
        st.session_state["draft"] = state.protocol().model_dump(
            mode="json", by_alias=True, exclude_none=True
        )
        st.session_state["draft_version"] = st.session_state.get("draft_version", 0) + 1
    return st.session_state["draft"]


def _reset_draft(data: dict | None = None) -> None:
    """Replace the draft and bump its version, so every widget re-reads its value."""
    if data is None:
        st.session_state.pop("draft", None)
    else:
        st.session_state["draft"] = data
    st.session_state["draft_version"] = st.session_state.get("draft_version", 0) + 1


def _key(*parts: object) -> str:
    return "ed-" + "-".join(str(p) for p in (st.session_state.get("draft_version", 0), *parts))


# ------------------------------------------------------------------ the tab


def protocol_tab() -> None:
    st.header("0 · Protocol")
    st.caption(
        "The protocol is the whole review: the search, the eligibility criteria Jev "
        "judges, and the thresholds that turn its probabilities into decisions. Each "
        "protocol keeps its own records and results."
    )

    _library_section()
    st.divider()

    spec = state.protocol()
    if not state.is_editable():
        st.info(
            f"**{spec.name}** is a bundled example and stays read-only, so it keeps working as "
            "a blueprint. Duplicate it above to make an editable copy."
        )
        _read_only_view(spec)
        return

    _edit_form()


def _library_section() -> None:
    create, upload, download = st.columns(3)

    with create.popover("➕ New protocol", use_container_width=True):
        sources = [("Blank starter", state.starter())] + [
            (f"Copy of {e.name}" + (" (example)" if e.bundled else ""), e.spec)
            for e in state.entries()
        ]
        pick = st.selectbox("Start from", range(len(sources)), format_func=lambda i: sources[i][0])
        name = st.text_input("Name", placeholder="e.g. Mindfulness apps for adolescent anxiety")
        if st.button("Create", type="primary", disabled=not name.strip()):
            state.create_protocol(sources[pick][1].model_copy(update={"name": name.strip()}, deep=True))
            st.rerun()

    with upload.popover("⬆️ Import YAML", use_container_width=True):
        uploaded = st.file_uploader("Protocol file", type=["yaml", "yml"])
        if uploaded is not None and st.button("Import", type="primary"):
            try:
                spec = library.parse_yaml(uploaded.getvalue().decode("utf-8"))
            except (ValueError, yaml.YAMLError, UnicodeDecodeError) as error:
                messages = (
                    validation_messages(error) if isinstance(error, ValueError) else [str(error)]
                )
                st.error("Not a valid protocol:\n\n" + "\n".join(f"- {m}" for m in messages))
            else:
                state.create_protocol(spec)
                st.rerun()

    download.download_button(
        "⬇️ Download YAML",
        library.to_yaml(state.protocol()),
        file_name=f"{state.current_id()}.yaml",
        mime="application/x-yaml",
        use_container_width=True,
    )

    if state.is_editable():
        with st.expander("Delete this protocol"):
            st.caption("Removes the protocol and its results from this session.")
            if st.checkbox("Yes, delete it", key="confirm_delete") and st.button("Delete"):
                state.delete_protocol(state.current_id())
                st.rerun()


def _read_only_view(spec: Protocol) -> None:
    st.markdown(f"### {spec.name}")
    if spec.description:
        st.write(spec.description)
    for kind in KINDS:
        items = [c for c in spec.criteria if c.kind == kind]
        if items:
            st.markdown(f"**{kind.capitalize()} criteria**")
            for criterion in items:
                st.caption(f"• **{criterion.label}** — {criterion.instructions}")
    if spec.choices:
        st.markdown("**Classifications**: " + ", ".join(f"`{c.id}`" for c in spec.choices))
    if spec.scores:
        st.markdown("**Scores**: " + ", ".join(f"`{s.id}`" for s in spec.scores))
    with st.expander("YAML"):
        st.code(library.to_yaml(spec), language="yaml")


def _edit_form() -> None:
    draft = _draft()

    basics, criteria, classify, raw = st.tabs(
        ["Basics & search", "Eligibility criteria", "Classifications & scores", "Raw YAML"]
    )
    with basics:
        _basics(draft)
    with criteria:
        _criteria(draft)
    with classify:
        _choices(draft)
        st.divider()
        _scores(draft)
    with raw:
        _raw_yaml(draft)

    st.divider()
    save, revert = st.columns([1, 1])
    if save.button("💾 Save protocol", type="primary", use_container_width=True):
        _save(draft)
    if revert.button("Discard changes", use_container_width=True):
        _reset_draft()
        st.rerun()


def _save(draft: dict) -> None:
    try:
        spec = Protocol.model_validate(draft)
    except (ValidationError, ValueError) as error:
        st.error(
            "Not saved — fix these first:\n\n"
            + "\n".join(f"- {m}" for m in validation_messages(error))
        )
        return
    state.save_protocol(spec)
    _reset_draft()
    st.success("Saved. Re-run screening for the changes to take effect.")


# ------------------------------------------------------------------ sections


def _basics(draft: dict) -> None:
    draft["name"] = st.text_input("Name", draft.get("name", ""), key=_key("name"))
    draft["description"] = st.text_area(
        "Description", draft.get("description", ""), key=_key("description"), height=80,
        help="For people reading the protocol. Not sent to Jev.",
    )
    draft["query"] = st.text_area(
        "Topical query", draft.get("query", ""), key=_key("query"), height=140,
        help="OpenAlex full-text search. Quoted phrases, AND / OR / NOT and parentheses "
             "work; commas do not.",
    )

    filters = draft.setdefault("filters", {})
    st.markdown("**Filters** — applied by OpenAlex and re-checked in code, never asked of Jev")
    a, b = st.columns(2)
    year_from = a.number_input(
        "From year (0 = no limit)", 0, 2100, int(filters.get("year_from") or 0), key=_key("yf")
    )
    year_to = b.number_input(
        "To year (0 = no limit)", 0, 2100, int(filters.get("year_to") or 0), key=_key("yt")
    )
    filters["year_from"] = int(year_from) or None
    filters["year_to"] = int(year_to) or None
    filters["languages"] = st.multiselect(
        "Languages", sorted(set(LANGUAGES) | set(filters.get("languages", []))),
        default=filters.get("languages", []), key=_key("lang"),
        accept_new_options=True, help="ISO 639-1 codes. Empty means any language.",
    )
    filters["types"] = st.multiselect(
        "Work types", sorted(set(WORK_TYPES) | set(filters.get("types", []))),
        default=filters.get("types", []), key=_key("types"),
        accept_new_options=True, help="OpenAlex work types. Empty means any type.",
    )
    filters["require_abstract"] = st.checkbox(
        "Drop records with no abstract", bool(filters.get("require_abstract")), key=_key("abs"),
        help="Off by default: a missing abstract is not evidence of ineligibility.",
    )

    thresholds = draft.setdefault("thresholds", {})
    st.markdown("**Thresholds**")
    c, d, e = st.columns(3)
    thresholds["include_min"] = c.slider(
        "Include ≥", 0.0, 1.0, float(thresholds.get("include_min", 0.6)), 0.01, key=_key("inc"),
        help="An inclusion criterion must reach this to count as met.",
    )
    thresholds["exclude_min"] = d.slider(
        "Exclude ≥", 0.0, 1.0, float(thresholds.get("exclude_min", 0.85)), 0.01, key=_key("exc"),
        help="An exclusion criterion must reach this to drop the record. Keep it high: "
             "screening is recall-critical.",
    )
    thresholds["informative_min"] = e.slider(
        "Informative ≥", 0.0, 1.0, float(thresholds.get("informative_min", 0.4)), 0.01,
        key=_key("inf"), help="Below this the abstract says too little — send to a human.",
    )


def _described_inputs(label: str, value: Any, key: str) -> Any:
    """Text + examples inputs for one outcome or level, or a note if it is too structured."""
    parts = split_described(value)
    if parts is None:
        st.caption(f"{label}: structured value — edit it in the Raw YAML tab.")
        return value
    what, examples = parts
    left, right = st.columns([3, 2])
    what = left.text_area(label, what, key=key + "-what", height=80)
    examples = right.text_area(
        "Examples (one per line)", "\n".join(examples), key=key + "-ex", height=80
    )
    return join_described(what, _lines(examples))


def _criteria(draft: dict) -> None:
    st.caption(
        "Each criterion is one yes/no question for Jev. State the exact condition, ask one "
        "thing at a time, and phrase it so that *yes* means the condition holds. Describing "
        "what a yes and a no look like is the most effective fix for a criterion that misfires."
    )
    items: list[dict] = draft.setdefault("criteria", [])
    remove: int | None = None

    for i, criterion in enumerate(items):
        title = f"`{criterion.get('kind', '?')}` · {criterion.get('label') or criterion.get('id') or 'untitled'}"
        with st.expander(title, expanded=not criterion.get("label")):
            a, b, c = st.columns([2, 1, 3])
            criterion["id"] = a.text_input("Id", criterion.get("id", ""), key=_key("c", i, "id"))
            kind = criterion.get("kind", "inclusion")
            criterion["kind"] = b.selectbox(
                "Kind", KINDS, KINDS.index(kind) if kind in KINDS else 0, key=_key("c", i, "kind")
            )
            criterion["label"] = c.text_input(
                "Label", criterion.get("label", ""), key=_key("c", i, "label"),
                help="Short name, reused verbatim as the PRISMA exclusion reason.",
            )
            criterion["instructions"] = st.text_area(
                "Question for Jev", criterion.get("instructions", ""),
                key=_key("c", i, "instr"), height=80,
            )
            outcomes = criterion.get("outcomes") or {}
            yes = _described_inputs("A *yes* means…", outcomes.get("true"), _key("c", i, "t"))
            no = _described_inputs("A *no* means…", outcomes.get("false"), _key("c", i, "f"))
            if yes is None and no is None:
                criterion.pop("outcomes", None)
            else:
                criterion["outcomes"] = {"true": yes, "false": no}
            criterion["sections"] = _sections_input(criterion, _key("c", i, "sec"))
            if st.button("Remove criterion", key=_key("c", i, "rm")):
                remove = i

    if st.button("➕ Add criterion"):
        items.append({"id": f"criterion_{len(items) + 1}", "kind": "inclusion", "label": "",
                      "instructions": ""})
        _reset_draft(draft)
        st.rerun()
    if remove is not None:
        items.pop(remove)
        _reset_draft(draft)
        st.rerun()


def _sections_input(item: dict, key: str) -> list[str]:
    return st.multiselect(
        "Full-text sections to read (stage 2)", SECTIONS,
        default=[s for s in item.get("sections", []) if s in SECTIONS], key=key,
        help="Which parts of the paper this question is answered from. Empty means "
             "title, abstract and methods.",
    )


def _choices(draft: dict) -> None:
    st.markdown("**Classifications** — closed-set questions that describe each record")
    st.caption("Not eligibility logic: they label records for reviewers and for breakdowns.")
    items: list[dict] = draft.setdefault("choices", [])
    remove: int | None = None

    for i, choice in enumerate(items):
        with st.expander(f"`{choice.get('id') or 'untitled'}`", expanded=not choice.get("id")):
            choice["id"] = st.text_input("Id", choice.get("id", ""), key=_key("ch", i, "id"))
            choice["instructions"] = st.text_input(
                "Question for Jev", choice.get("instructions", ""), key=_key("ch", i, "instr")
            )
            # The editor must be fed the same frame on every rerun, or it drops its edits:
            # snapshot the options once per draft version and let it track changes.
            source_key = _key("ch", i, "opts-src")
            if source_key not in st.session_state:
                st.session_state[source_key] = pd.DataFrame(
                    [{"option": k, "description": v if isinstance(v, str) else ""}
                     for k, v in (choice.get("options") or {}).items()],
                    columns=["option", "description"],
                )
            edited = st.data_editor(
                st.session_state[source_key], num_rows="dynamic", use_container_width=True,
                hide_index=True, key=_key("ch", i, "opts"),
            )
            choice["options"] = {
                str(row["option"]).strip(): (str(row["description"]).strip() or None)
                if pd.notna(row["description"]) else None
                for _, row in edited.iterrows()
                if pd.notna(row["option"]) and str(row["option"]).strip()
            }
            choice["sections"] = _sections_input(choice, _key("ch", i, "sec"))
            if st.button("Remove classification", key=_key("ch", i, "rm")):
                remove = i

    if st.button("➕ Add classification"):
        items.append({"id": f"classification_{len(items) + 1}", "instructions": "",
                      "options": {"yes": None, "no": None}})
        _reset_draft(draft)
        st.rerun()
    if remove is not None:
        items.pop(remove)
        _reset_draft(draft)
        st.rerun()


def _scores(draft: dict) -> None:
    st.markdown("**Scores** — ordered ratings, lowest level first")
    items: list[dict] = draft.setdefault("scores", [])
    ids = [s.get("id") for s in items if s.get("id")]
    if ids:
        current = draft.get("ranking_score")
        options = ["(automatic)"] + ids
        picked = st.selectbox(
            "Score that ranks the review queue", options,
            options.index(current) if current in ids else 0, key=_key("rank"),
            help="Automatic uses `topical_fit` if present, else the first Score.",
        )
        draft["ranking_score"] = None if picked == "(automatic)" else picked
    else:
        draft.pop("ranking_score", None)

    remove: int | None = None
    for i, score in enumerate(items):
        with st.expander(f"`{score.get('id') or 'untitled'}`", expanded=not score.get("id")):
            score["id"] = st.text_input("Id", score.get("id", ""), key=_key("s", i, "id"))
            score["instructions"] = st.text_input(
                "Question for Jev", score.get("instructions", ""), key=_key("s", i, "instr")
            )
            levels = score.get("levels") or []
            new_levels = []
            for j, level in enumerate(levels):
                new_levels.append(_described_inputs(f"Level {j}", level, _key("s", i, "l", j)))
            score["levels"] = [lv for lv in new_levels if lv is not None]
            add_level, drop_level = st.columns(2)
            if add_level.button("➕ Level", key=_key("s", i, "add"), disabled=len(levels) >= 10):
                score["levels"].append("")
                _reset_draft(draft)
                st.rerun()
            if drop_level.button("➖ Last level", key=_key("s", i, "pop"), disabled=len(levels) <= 2):
                score["levels"].pop()
                _reset_draft(draft)
                st.rerun()
            score["sections"] = _sections_input(score, _key("s", i, "sec"))
            if st.button("Remove score", key=_key("s", i, "rm")):
                remove = i

    if st.button("➕ Add score"):
        items.append({"id": f"score_{len(items) + 1}", "instructions": "",
                      "levels": ["Low", "High"]})
        _reset_draft(draft)
        st.rerun()
    if remove is not None:
        items.pop(remove)
        if draft.get("ranking_score") not in [s.get("id") for s in items]:
            draft.pop("ranking_score", None)
        _reset_draft(draft)
        st.rerun()


def _raw_yaml(draft: dict) -> None:
    st.caption(
        "The whole draft as YAML, for anything the form does not cover. Apply loads it back "
        "into the form; Save below still validates before anything is written."
    )
    text = st.text_area(
        "YAML",
        yaml.safe_dump(draft, sort_keys=False, allow_unicode=True, width=100),
        height=480, key=_key("raw"), label_visibility="collapsed",
    )
    if st.button("Apply YAML to the form"):
        try:
            parsed = yaml.safe_load(text)
        except yaml.YAMLError as error:
            st.error(f"Invalid YAML: {error}")
            return
        if not isinstance(parsed, dict):
            st.error("The YAML must be a mapping at the top level.")
            return
        _reset_draft(parsed)
        st.rerun()
