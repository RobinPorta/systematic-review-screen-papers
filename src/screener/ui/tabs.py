"""The pipeline tabs, in order. The Protocol tab lives in `editor`.

Each one is a thin view over `screener.pipeline`. Nothing here decides anything about a
paper — that is the point of the verification step that runs the CLI and the UI and
compares their numbers.
"""

from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

from .. import report
from ..answers import ChoiceValue, NoulValue, ScoreValue
from ..config import MissingCredential
from ..decide import Label
from ..evaluate import evaluate
from ..pipeline import (
    check_coverage,
    count_results,
    fetch_and_parse,
    screen_abstracts,
    screen_full_texts,
    search,
    shortlist,
)
from ..prisma import build_flow
from ..query import build_filter, filter_for
from ..sampling import stratified_sample
from ..screen import estimate_cost
from ..openalex import OpenAlexError
from ..tune import sweep
from . import state

LABEL_COLOURS = {
    Label.INCLUDE.value: "#2e7d32",
    Label.MAYBE.value: "#ef6c00",
    Label.EXCLUDE.value: "#757575",
    Label.NOT_RETRIEVABLE.value: "#8e24aa",
}


def _progress(container, noun: str):
    bar = container.progress(0.0, text=f"{noun}…")

    def callback(done: int, total: int) -> None:
        if total:
            bar.progress(min(done / total, 1.0), text=f"{noun}: {done}/{total}")

    return callback


def _download_csv(kind: str, what: str) -> None:
    text = state.download(kind)
    if text:
        st.download_button(
            f"⬇️ Download {what} (CSV)", text, file_name=f"{state.current_id()}_{kind}.csv",
            mime="text/csv",
        )


def _counts_row(counts: dict[str, int]) -> None:
    columns = st.columns(max(len(counts), 1))
    for column, (name, value) in zip(columns, counts.items()):
        column.metric(name.replace("_", " "), value)


# ---------------------------------------------------------------- 1. Search


def search_tab() -> None:
    st.header("1 · Search")
    st.caption(
        "Year, language and work type are applied in the OpenAlex filter and re-checked "
        "in code — never asked of Jev, which is documented as unreliable at comparing dates."
    )

    spec = state.protocol()

    spec.query = st.text_area(
        "Topical query", spec.query, height=160,
        help="Quoted phrases, AND / OR / NOT and parentheses all work. Commas do not — "
             "OpenAlex reads a comma as a filter separator.",
    )

    left, middle, right = st.columns([1, 1, 2])
    spec.filters.year_from = left.number_input(
        "From year", min_value=1900, max_value=2100, value=spec.filters.year_from or 1990
    )
    to_year = middle.number_input(
        "To year (0 = no limit)", min_value=0, max_value=2100, value=spec.filters.year_to or 0
    )
    spec.filters.year_to = int(to_year) or None
    spec.filters.languages = right.multiselect(
        "Languages",
        ["en", "it", "fr", "de", "es", "pt", "nl"],
        default=spec.filters.languages or ["en"],
        help="ISO 639-1 codes, as OpenAlex reports them.",
    )

    assembled = build_filter(spec.query, spec.filters)
    st.text_area("Assembled OpenAlex filter", assembled, height=110, disabled=True)
    if spec.filters.year_from:
        st.caption(
            f"`from_publication_date:{spec.filters.year_from}-01-01` — OpenAlex date bounds "
            "are inclusive, so there is no off-by-one to get wrong here."
        )
    if spec.filters.languages:
        st.caption(
            "OpenAlex returns a `language` field on every work, so unlike a Scopus "
            "LANGUAGE() clause this filter is re-checked in code after fetching."
        )

    if state.is_editable():
        if st.button("Save query and filters to the protocol"):
            state.save_protocol(spec)
            st.success("Saved.")
    else:
        st.caption(
            "Edits here apply to this session only — this is a bundled example. "
            "Duplicate it on the Protocol tab to keep them."
        )

    count_button, fetch_button, limit_column = st.columns([1, 1, 2], vertical_alignment="bottom")
    limit = limit_column.number_input("Fetch at most", min_value=1, max_value=5000, value=200)

    if count_button.button("How many results?", use_container_width=True):
        try:
            state.run().result_count = count_results(spec, settings=state.settings())
        except (OpenAlexError, MissingCredential) as error:
            st.error(str(error))

    if state.run().result_count is not None:
        st.info(f"{state.run().result_count:,} works match this query.")

    if fetch_button.button("Fetch records", type="primary", use_container_width=True):
        container = st.empty()
        try:
            outcome = search(
                spec,
                settings=state.settings(),
                limit=int(limit),
                on_page=_progress(container, "fetched"),
            )
        except (OpenAlexError, MissingCredential) as error:
            container.empty()
            st.error(str(error))
            return

        container.empty()
        state.set_records(outcome.kept)
        st.success(f"Fetched {outcome.fetched}, kept {outcome.total} after filtering.")
        if outcome.duplicates:
            st.caption(f"{outcome.duplicates} duplicates removed.")
        for reason, count in sorted(outcome.dropped.items(), key=lambda kv: -kv[1]):
            st.caption(f"{count} dropped — {reason}")
        if outcome.without_abstract:
            st.caption(
                f"{outcome.without_abstract} kept with no abstract — they will fail the "
                "informative guard and go to a human rather than being dropped."
            )
        if outcome.credits_remaining is not None:
            st.caption(f"OpenAlex credits remaining: {outcome.credits_remaining:,}")

    records = state.records()
    if records:
        st.subheader(f"{len(records)} records ready")
        st.dataframe(
            pd.DataFrame(
                [
                    {"year": r.year, "title": r.title, "journal": r.journal,
                     "abstract": "yes" if r.has_abstract else "MISSING"}
                    for r in records[:200]
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )


# ---------------------------------------------------------------- 2. Screen


def screen_tab() -> None:
    st.header("2 · Screen abstracts")
    st.caption(
        "Every question goes into a single Jev call per record. Questions in one request "
        "are answered in parallel, so asking one you might not need is nearly free."
    )

    spec = state.protocol()
    records = state.records()
    if not records:
        st.info("Fetch some records on the Search tab first.")
        return

    with st.expander(f"The {len(spec.criteria)} eligibility criteria", expanded=False):
        for criterion in spec.criteria:
            st.markdown(f"**{criterion.label}** · `{criterion.kind}`")
            st.caption(criterion.instructions)
            if criterion.outcomes:
                st.json(criterion.outcomes.as_criteria(), expanded=False)

    left, right = st.columns(2)
    limit = left.number_input("Screen at most", 1, len(records), min(len(records), 200))
    concurrency = right.slider("Concurrent calls", 1, 24, 8)

    batch = records[: int(limit)]
    tokens, usd = estimate_cost(spec, batch)
    st.info(f"{len(batch)} records · ~{tokens:,} input tokens · ~${usd:.4f}")

    if st.button("Run stage 1", type="primary"):
        container = st.empty()
        try:
            run = screen_abstracts(
                spec, batch, settings=state.settings(), **state.jev_cache(),
                concurrency=int(concurrency), on_progress=_progress(container, "screened"),
            )
        except MissingCredential as error:
            container.empty()
            st.error(str(error))
            return

        container.empty()
        state.set_screen_run(run)

    run = state.screen_run()
    if run and run.results:
        st.subheader("Result")
        _counts_row(run.counts())
        st.caption(report.format_usage(run.usage))
        _download_csv("screened", "screening results")
        failures = [r for r in run.results if r.error]
        if failures:
            st.warning(
                f"{len(failures)} call(s) failed and were sent for human review rather than "
                f"guessed at. First error: {failures[0].error}"
            )


# ---------------------------------------------------------------- 3. Review


def _probability_chart(spec, values: dict) -> alt.Chart | None:
    rows = []
    for criterion in spec.criteria:
        value = values.get(criterion.id)
        if isinstance(value, NoulValue):
            rows.append({"criterion": criterion.label, "p": value.noul, "kind": criterion.kind})
    if not rows:
        return None

    frame = pd.DataFrame(rows)
    return (
        alt.Chart(frame)
        .mark_bar()
        .encode(
            x=alt.X("p:Q", scale=alt.Scale(domain=[0, 1]), title="probability"),
            y=alt.Y("criterion:N", sort=None, title=None),
            color=alt.Color(
                "kind:N",
                scale=alt.Scale(
                    domain=["inclusion", "exclusion", "guard"],
                    range=["#2e7d32", "#c62828", "#0277bd"],
                ),
                legend=alt.Legend(title=None, orient="bottom"),
            ),
            tooltip=["criterion", alt.Tooltip("p:Q", format=".3f")],
        )
        .properties(height=max(120, 26 * len(rows)))
    )


def review_tab() -> None:
    st.header("3 · Review")
    spec = state.protocol()
    run = state.screen_run()
    if run is None or not run.results:
        st.info("Run stage 1 on the Screen tab first.")
        return

    chosen = st.multiselect(
        "Show bands",
        [label.value for label in Label],
        default=[Label.INCLUDE.value, Label.MAYBE.value],
    )
    rows = [r for r in run.results if r.decision.label.value in chosen]
    rows.sort(key=lambda r: r.decision.rank, reverse=True)
    if not rows:
        st.info("No records in the selected bands.")
        return

    st.caption(f"{len(rows)} records, highest-ranked first.")
    st.dataframe(
        pd.DataFrame([
            {
                "label": r.decision.label.value,
                "rank": round(r.decision.rank, 3),
                "year": r.record.year,
                "title": r.record.title[:110],
            }
            for r in rows[:300]
        ]),
        use_container_width=True,
        hide_index=True,
    )

    options = {f"[{r.decision.label.value}] {r.record.title[:90]}": r for r in rows[:300]}
    selected = options[st.selectbox("Inspect a record", list(options))]

    left, right = st.columns([3, 2])
    with left:
        st.markdown(f"### {selected.record.title}")
        st.caption(
            f"{selected.record.journal or 'unknown journal'} · {selected.record.year or 'n.d.'}"
            + (f" · [doi]({'https://doi.org/' + selected.record.doi})" if selected.record.doi else "")
        )
        st.write(selected.record.abstract or "_No abstract._")
        if selected.record.keywords:
            st.caption("Keywords: " + ", ".join(selected.record.keywords))

    with right:
        colour = LABEL_COLOURS.get(selected.decision.label.value, "#555")
        st.markdown(
            f"<h3 style='color:{colour};margin-top:0'>{selected.decision.label.value.upper()}</h3>",
            unsafe_allow_html=True,
        )
        values = selected.answer_values
        chart = _probability_chart(spec, values)
        if chart is not None:
            st.altair_chart(chart, use_container_width=True)

        st.markdown("**Why**")
        for reason in selected.decision.deciding_reasons:
            st.caption(f"• {reason.describe()}")

        classifications = {
            choice.id: values[choice.id]
            for choice in spec.choices
            if isinstance(values.get(choice.id), ChoiceValue)
        }
        if classifications:
            st.markdown("**Classified as**")
            for question_id, value in classifications.items():
                st.caption(f"{question_id}: **{value.choice}** (confidence {value.confidence:.2f})")

        for score in spec.scores:
            value = values.get(score.id)
            if isinstance(value, ScoreValue):
                st.caption(
                    f"{score.id}: {value.score:.2f} of {score.top_level} "
                    f"(confidence {value.confidence:.2f})"
                )


# -------------------------------------------------------------- 4. Full text


def fulltext_tab() -> None:
    st.header("4 · Full text")
    st.caption(
        "Each criterion is routed to the sections that bear on it, so a paper is never sent "
        "whole — Jev's accuracy falls as the state fills with text unrelated to the decision."
    )

    spec = state.protocol()
    stage1 = state.screen_run()
    if stage1 is None or not stage1.results:
        st.info("Run stage 1 on the Screen tab first.")
        return

    candidates = shortlist(stage1)
    st.write(f"**{len(candidates)}** records advanced from stage 1 (include + maybe).")

    left, right = st.columns(2, vertical_alignment="bottom")
    if left.button("Check coverage first", use_container_width=True):
        container = st.empty()
        try:
            coverage, _ = check_coverage(
                candidates, settings=state.settings(), on_progress=_progress(container, "checked")
            )
        except MissingCredential as error:
            container.empty()
            st.error(str(error))
            return
        container.empty()
        state.run().coverage = coverage

    coverage = state.run().coverage
    if coverage:
        st.metric(
            "Full texts obtainable",
            f"{coverage.retrievable}/{coverage.total}",
            f"{coverage.percentage:.0f}%",
        )
        st.json(coverage.by_status, expanded=False)
        if coverage.percentage < 50:
            st.warning(
                "Europe PMC holds full text for its biomedical open-access subset only, so a "
                "low number here is expected rather than a fault for any topic outside the "
                "life sciences. Everything it cannot supply is reported as PRISMA's "
                "'reports not retrieved'."
            )

    limit = right.number_input("Assess at most", 1, max(len(candidates), 1), min(len(candidates), 50))

    if st.button("Run stage 2", type="primary"):
        batch = candidates[: int(limit)]
        container = st.empty()
        try:
            items = fetch_and_parse(
                batch, settings=state.settings(), on_progress=_progress(container, "fetched")
            )
            run = screen_full_texts(
                spec, items, settings=state.settings(), **state.jev_cache(),
                on_progress=_progress(container, "assessed"),
            )
        except MissingCredential as error:
            container.empty()
            st.error(str(error))
            return

        container.empty()
        state.set_fulltext_run(run)

    run = state.fulltext_run()
    if not run or not run.results:
        return

    st.subheader("Result")
    _counts_row(run.counts())
    st.caption(report.format_usage(run.usage, title="Jev usage (stage 2)"))
    _download_csv("fulltext", "full-text results")

    reasons = run.exclusion_reasons()
    if reasons:
        st.markdown("**Exclusion reasons** — PRISMA requires full-text exclusions to be reported with these.")
        st.dataframe(
            pd.DataFrame(sorted(reasons.items(), key=lambda kv: -kv[1]), columns=["reason", "n"]),
            use_container_width=True,
            hide_index=True,
        )

    assessed = [r for r in run.results if r.section_map]
    if not assessed:
        return

    options = {f"[{r.decision.label.value}] {r.record.title[:90]}": r for r in assessed}
    selected = options[st.selectbox("Inspect a paper", list(options))]

    left, right = st.columns([2, 3])
    with left:
        st.markdown("**Sections parsed**")
        st.dataframe(
            pd.DataFrame([
                {
                    "heading": s.title[:48],
                    "mapped to": s.canonical or "—",
                    "by": "Jev" if s.inferred else "regex",
                    "tokens": s.tokens,
                }
                for s in selected.section_map
            ]),
            use_container_width=True,
            hide_index=True,
            height=340,
        )
    with right:
        st.markdown(f"**Decision: {selected.decision.label.value.upper()}**")
        if selected.decision.exclusion_reason:
            st.caption(f"Reason: {selected.decision.exclusion_reason}")
        st.caption(f"{selected.calls} Jev calls · {selected.input_tokens:,} input tokens")
        st.markdown("**What each judgement read**")
        for reason in selected.decision.reasons:
            st.caption(f"• {reason.describe()}")


# ----------------------------------------------------------------- 5. Label


def label_tab() -> None:
    st.header("5 · Label")
    st.caption(
        "A stratified sample across the three bands. Sampling at random from a corpus that "
        "is a few percent eligible would be almost all obvious excludes, and would say "
        "nothing about the error that matters."
    )

    stage = st.radio("Stage", ["abstracts", "fulltext"], horizontal=True)
    run = state.screen_run() if stage == "abstracts" else state.fulltext_run()
    if run is None or not run.results:
        st.info(f"No {stage} results yet.")
        return

    size = st.number_input("Sample size", 5, min(len(run.results), 500), min(len(run.results), 50))
    if st.button("Draw sample"):
        picked = stratified_sample(
            [(r.record.id, r.decision.label, r.decision.rank) for r in run.results],
            size=int(size),
        )
        drawn = state.draw_labels(stage, run, picked)
        st.success(f"Drew {drawn} records to label.")

    frame = state.labels(stage)
    if frame is None:
        st.info("Draw a sample to start labelling.")
        return

    existing = state.gold(stage)
    st.download_button(
        "⬇️ Download labels (CSV)", frame.to_csv(index=False), file_name=f"labels_{stage}.csv",
        mime="text/csv",
    )
    unlabelled = frame[frame["gold_label"].str.strip() == ""]
    st.progress(
        len(existing) / max(len(frame), 1),
        text=f"{len(existing)} of {len(frame)} labelled",
    )

    if unlabelled.empty:
        st.success("Sample fully labelled. Head to the Evaluate tab.")
        return

    # Skipped records go to the back of the queue for this session, not away for good.
    skipped = state.skipped(stage)
    pending = unlabelled[~unlabelled["record_id"].isin(skipped)]
    if pending.empty:
        st.info(f"You skipped the remaining {len(unlabelled)} — starting over with them.")
        skipped.clear()
        pending = unlabelled

    row = pending.iloc[0]
    st.markdown(f"### {row['title']}")
    st.caption(f"{row.get('journal', '')} · {row.get('year', '')} · screener said **{row['predicted']}**")
    st.write(row["abstract"] or "_No abstract._")

    include, exclude, skip = st.columns(3)

    def record_label(value: str) -> None:
        frame.loc[frame["record_id"] == row["record_id"], "gold_label"] = value
        state.set_labels(stage, frame)
        st.rerun()

    if include.button("Include", type="primary", use_container_width=True):
        record_label("include")
    if exclude.button("Exclude", use_container_width=True):
        record_label("exclude")
    if skip.button("Skip", use_container_width=True):
        skipped.add(row["record_id"])
        st.rerun()

    st.caption(f"You are the ground truth here — label as your protocol says, not as the screener did.")


# -------------------------------------------------------------- 6. Evaluate


def evaluate_tab() -> None:
    st.header("6 · Evaluate")
    spec = state.protocol()

    stage = st.radio("Stage", ["abstracts", "fulltext"], horizontal=True, key="eval_stage")
    run = state.screen_run() if stage == "abstracts" else state.fulltext_run()

    if run is None or not run.results:
        st.info(f"No {stage} results yet.")
        return
    if state.labels(stage) is None:
        st.info("Label a calibration sample first.")
        return

    gold = state.gold(stage)
    metrics = evaluate(((r.record.id, r.decision.label) for r in run.results), gold)
    if not metrics.n:
        st.info("No labels filled in yet.")
        return

    a, b, c, d = st.columns(4)
    a.metric("Recall", f"{metrics.recall:.1%}", help="Eligible studies the screener kept")
    b.metric("Precision", f"{metrics.precision:.1%}")
    c.metric("Auto-excluded", f"{metrics.auto_excluded:.1%}", help="Reading avoided")
    d.metric("Workload saved", f"{metrics.workload_saved:.1%}")

    if metrics.missed:
        st.error(
            f"{metrics.missed} eligible stud{'y was' if metrics.missed == 1 else 'ies were'} "
            "auto-excluded. This is the error that matters — raise `exclude_min` or sharpen "
            "the criterion that fired."
        )
    else:
        st.success(f"No eligible studies were auto-excluded in this sample of {metrics.n}.")

    st.dataframe(
        pd.DataFrame(metrics.confusion().items(), columns=["outcome", "n"]),
        use_container_width=True,
        hide_index=True,
    )

    st.subheader("Threshold sweep")
    st.caption(
        "Free: every probability from the run is stored, so re-deciding the corpus under "
        "different thresholds is arithmetic, not API calls."
    )
    target_recall = st.slider("Target recall", 0.80, 1.0, 0.95, 0.01)

    cases = [(r.record.id, r.answer_values) for r in run.results if r.answers]
    result = sweep(spec, cases, gold, target_recall=target_recall)
    if not result.points:
        st.info("Not enough labelled data to sweep.")
        return

    frame = pd.DataFrame([p.model_dump() for p in result.points])
    chart = (
        alt.Chart(frame)
        .mark_circle(size=70, opacity=0.7)
        .encode(
            x=alt.X("recall:Q", scale=alt.Scale(domain=[0, 1]), title="recall"),
            y=alt.Y("auto_excluded:Q", scale=alt.Scale(domain=[0, 1]), title="auto-excluded"),
            color=alt.Color("workload_saved:Q", title="WSS", scale=alt.Scale(scheme="viridis")),
            tooltip=["include_min", "exclude_min", "recall", "precision", "auto_excluded", "missed"],
        )
    )
    rule = alt.Chart(pd.DataFrame({"recall": [target_recall]})).mark_rule(
        color="red", strokeDash=[6, 4]
    ).encode(x="recall:Q")
    st.altair_chart(chart + rule, use_container_width=True)

    best = result.best
    if best and result.meeting_target():
        st.success(
            f"Best setting holding {target_recall:.0%} recall: **{best.label}** — "
            f"recall {best.recall:.1%}, auto-excluded {best.auto_excluded:.1%}, "
            f"{best.missed} eligible missed."
        )
        if not state.is_editable():
            st.caption(
                "This is a bundled example, so the thresholds cannot be written back. "
                "Duplicate it on the Protocol tab to keep tuned thresholds."
            )
        elif st.button("Write these thresholds to the protocol"):
            spec.thresholds.include_min = best.include_min
            spec.thresholds.exclude_min = best.exclude_min
            state.save_protocol(spec)
            st.success("Thresholds saved to the protocol.")
    else:
        st.warning(
            "No setting holds the target recall. The honest conclusion is that the criteria "
            "need sharpening, not that the target needs lowering."
        )

    st.subheader("PRISMA flow")
    identified = len(state.records()) or len(run.results)
    flow = build_flow(
        identified=identified,
        stage1=state.screen_run(),
        stage2=state.fulltext_run(),
    )
    st.code(flow.render(), language="text")
