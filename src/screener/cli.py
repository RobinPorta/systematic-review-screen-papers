"""Command line for the screening pipeline.

    screener doctor      check the setup before anything else
    screener demo        screen ten bundled records — needs no search API at all
    screener search      fetch records from OpenAlex
    screener screen      stage 1 — title and abstract
    screener fulltext    stage 2 — full text, section by section
    screener sections    dump how one paper parsed, for debugging the routing
    screener label       draw the calibration sample to hand-screen
    screener evaluate    score a stage against those labels
    screener tune        sweep thresholds for the best workload at a target recall
    screener prisma      the flow diagram counts
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import typer

from . import paths, report
from .config import MissingCredential, ensure_dirs, load_settings
from .evaluate import format_metrics
from .evaluate import evaluate as evaluate_metrics
from .fulltext.europepmc import EuropePMCSource
from .fulltext.sections import parse_sections
from .openalex import OpenAlexError
from .pipeline import (
    check_coverage,
    count_results,
    fetch_and_parse,
    load_protocol,
    screen_abstracts,
    screen_full_texts,
    search,
    shortlist,
)
from .prisma import build_flow
from .protocol import Protocol
from .query import filter_for
from .records import Record, read_jsonl, write_jsonl
from .sampling import stratified_sample
from .screen import ScreenRun, estimate_cost
from .screen_fulltext import FullTextRun
from .tune import format_sweep, sweep

app = typer.Typer(add_completion=False, help=__doc__, no_args_is_help=True)

ProtocolOption = typer.Option(paths.DEFAULT_PROTOCOL, "--protocol", "-p", help="Protocol YAML.")


def _echo(message: str = "") -> None:
    typer.echo(message)


def _load(protocol_path: Path) -> Protocol:
    try:
        return load_protocol(protocol_path)
    except FileNotFoundError:
        raise typer.BadParameter(f"no protocol at {protocol_path}") from None
    except ValueError as error:
        raise typer.BadParameter(f"{protocol_path} is not a valid protocol: {error}") from None


def _settings():
    try:
        return load_settings()
    except MissingCredential as error:
        _echo(str(error))
        raise typer.Exit(code=1) from None


def _progress(noun: str):
    def callback(done: int, total: int) -> None:
        if total and (done == total or done % 10 == 0 or total <= 20):
            _echo(f"  {noun}: {done}/{total}")

    return callback


def _run_and_gold(
    protocol: Path, stage: str, gold: Optional[Path]
) -> tuple[ScreenRun | FullTextRun, Path]:
    """The stage's results and its gold labels, from the protocol's run directory."""
    run_dir = paths.run_paths(protocol)
    if stage == "abstracts":
        source, default_gold, kind = run_dir.screened, run_dir.labels_stage1, ScreenRun
    elif stage == "fulltext":
        source, default_gold, kind = run_dir.fulltext, run_dir.labels_stage2, FullTextRun
    else:
        raise typer.BadParameter("stage must be 'abstracts' or 'fulltext'")
    if not source.exists():
        _echo(f"no {stage} results at {source}")
        raise typer.Exit(code=1)
    return kind.read_jsonl(source), gold or default_gold


@app.callback()
def main(verbose: bool = typer.Option(False, "--verbose", "-v", help="Log SDK and HTTP detail.")) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    ensure_dirs()


@app.command()
def query(protocol: Path = ProtocolOption) -> None:
    """Print the assembled OpenAlex filter without running it."""
    _echo(filter_for(_load(protocol)))


#: An open-access paper held in Europe PMC, used only to probe full-text retrieval.
_PROBE_DOI = "10.1371/journal.pone.0118173"


@app.command()
def doctor() -> None:
    """Check the setup and report exactly what is and is not working.

    Worth running before anything else. Only one credential is needed — OpenAlex and
    Europe PMC require no key, so most of what can go wrong here is a missing TypeSafe
    key or no network.
    """
    from typesafe_sdk import TypeSafeClient, TypeSafeError

    from .fulltext.base import RetrievalStatus
    from .openalex import OpenAlexClient

    settings = load_settings()
    problems = 0

    _echo("TypeSafe (Jev)  — the one credential this project needs")
    if not settings.typesafe_api_key:
        _echo("  ✗ TYPESAFE_API_KEY is not set — see Credentials in the README")
        problems += 1
    else:
        try:
            with TypeSafeClient(api_key=settings.typesafe_api_key) as client:
                models = [m.name for m in client.models.list().models]
            _echo(f"  ✓ key accepted; models available: {', '.join(models) or '(none listed)'}")
            _echo(f"  · default model: {settings.typesafe_model or 'jev-latest'}")
        except TypeSafeError as error:
            _echo(f"  ✗ {error}")
            problems += 1

    _echo("\nOpenAlex (search + abstracts)  — no key required")
    _echo(f"  · contact address: {settings.openalex_mailto or 'not set (polite pool disabled)'}")
    try:
        with OpenAlexClient(settings) as client:
            total = client.count('title_and_abstract.search:"systematic review"')
            # page_size=1 as well as limit=1: the client pages 200 at a time by default,
            # and a diagnostic has no business pulling 200 records to look at one.
            sample = next(iter(client.search(
                'title_and_abstract.search:"systematic review",has_abstract:true',
                limit=1, page_size=1, use_cache=False,
            )), None)
            credits = client.credits_remaining
        _echo(f"  ✓ reachable; the probe query matches {total:,} works")
        if sample is not None and sample.has_abstract:
            _echo(f"  ✓ abstracts are being returned ({len(sample.abstract)} chars on the sample)")
        else:
            _echo("  ? the probe returned no abstract, which is unexpected")
        if credits is not None:
            _echo(f"  · rate-limit credits remaining: {credits:,}")
    except Exception as error:  # noqa: BLE001 - the report is the point, not the type
        _echo(f"  ✗ {error}")
        problems += 1

    _echo("\nEurope PMC (full text)  — no key required")
    try:
        with EuropePMCSource(settings) as source:
            status = source.check(Record(id=_PROBE_DOI, doi=_PROBE_DOI))
        if status is RetrievalStatus.OK:
            _echo(f"  ✓ full text retrievable (probed {_PROBE_DOI})")
        else:
            _echo(f"  ? probe returned {status.value}")
    except Exception as error:  # noqa: BLE001
        _echo(f"  ✗ {error}")

    _echo("")
    if problems:
        _echo(f"{problems} problem(s) to fix. See Credentials and Troubleshooting in the README.")
        raise typer.Exit(code=1)
    _echo("All good. Try `screener demo`, then `screener search --count`.")


@app.command("search")
def search_cmd(
    protocol: Path = ProtocolOption,
    limit: Optional[int] = typer.Option(None, "--limit", "-n", help="Stop after this many records."),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Default: the protocol's run directory."),
    count_only: bool = typer.Option(False, "--count", help="Only report how many results match."),
    no_cache: bool = typer.Option(False, "--no-cache", help="Bypass the cached OpenAlex pages."),
) -> None:
    """Run the protocol's query against OpenAlex."""
    spec = _load(protocol)
    out = out or paths.run_paths(protocol).records
    settings = _settings()
    _echo(f"filter: {filter_for(spec)}")

    try:
        if count_only:
            _echo(f"{count_results(spec, settings=settings):,} matching works")
            return

        outcome = search(
            spec, settings=settings, limit=limit, use_cache=not no_cache,
            on_page=_progress("fetched"),
        )
    except (OpenAlexError, MissingCredential) as error:
        _echo(f"\n{error}")
        raise typer.Exit(code=1) from None

    write_jsonl(outcome.kept, out)
    _echo(f"\nfetched {outcome.fetched}, kept {outcome.total} -> {out}")
    if outcome.duplicates:
        _echo(f"  {outcome.duplicates} duplicates removed")
    for reason, count in sorted(outcome.dropped.items(), key=lambda kv: -kv[1]):
        _echo(f"  {count} dropped: {reason}")
    if outcome.without_abstract:
        _echo(
            f"  {outcome.without_abstract} kept with no abstract — these will fail the "
            "informative guard and go to a human, not be dropped"
        )
    if outcome.credits_remaining is not None:
        _echo(f"  OpenAlex credits remaining: {outcome.credits_remaining:,}")


@app.command()
def demo(
    protocol: Path = ProtocolOption,
    write_labels: bool = typer.Option(
        False, "--write-labels", help="Also write a gold-label template to correct by hand."
    ),
    no_cache: bool = typer.Option(False, "--no-cache"),
) -> None:
    """Screen ten bundled example records against the real model.

    No search API needed — the records ship with the project. Useful for watching
    how the probabilities move before pointing the pipeline at a real search, and for
    checking a criterion you have just reworded.
    """
    spec = _load(protocol)
    source = Path("examples/sample_records.jsonl")
    if not source.exists():
        _echo(f"no example records at {source}")
        raise typer.Exit(code=1)

    items = list(read_jsonl(source))
    settings = _settings()
    run = screen_abstracts(spec, items, settings=settings, use_cache=not no_cache)

    for result in run.results:
        if result.error:
            _echo(f"\n{result.record.title[:90]}\n  ERROR {result.error}")
            continue
        _echo("\n" + "=" * 92)
        _echo(result.record.title[:92])
        _echo(f"  -> {result.decision.label.value.upper():<10} rank={result.decision.rank:.2f}")
        for reason in result.decision.reasons:
            mark = {"met": "ok  ", "not_met": "    ", "fired": "FIRE", "uncertain": "?   "}[
                reason.verdict.value
            ]
            _echo(f"     {mark} {reason.label:<44} p={reason.probability:.3f}")
        values = result.answer_values
        for choice in spec.choices:
            answer = values.get(choice.id)
            if answer is not None:
                _echo(f"     .    {choice.id:<44} {answer.choice} ({answer.confidence:.2f})")
        if result.decision.exclusion_reason:
            _echo(f"     reason: {result.decision.exclusion_reason}")

    _echo("\n" + report.format_counts(run.counts()))
    _echo(report.format_usage(run.usage))

    run_dir = paths.run_paths(protocol)
    run.write_jsonl(run_dir.screened)
    report.write_screening_csv(spec, run, run_dir.screened_csv)
    _echo(f"-> {run_dir.screened} and {run_dir.screened_csv}")

    if write_labels:
        written = report.write_label_template(
            run, [r.record.id for r in run.results], run_dir.labels_stage1
        )
        _echo(f"-> {run_dir.labels_stage1} ({written} rows)")
        _echo(
            "The gold_label column is empty on purpose. Fill it in yourself — grading a "
            "screener against its own output measures nothing."
        )


@app.command()
def screen(
    protocol: Path = ProtocolOption,
    records: Optional[Path] = typer.Option(None, "--records", "-r"),
    out: Optional[Path] = typer.Option(None, "--out", "-o"),
    limit: Optional[int] = typer.Option(None, "--limit", "-n"),
    concurrency: int = typer.Option(8, "--concurrency", "-c"),
    no_cache: bool = typer.Option(False, "--no-cache"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Estimate cost without calling Jev."),
) -> None:
    """Stage 1: screen titles and abstracts. One Jev call per record."""
    spec = _load(protocol)
    run_dir = paths.run_paths(protocol)
    records = records or run_dir.records
    out = out or run_dir.screened
    if not records.exists():
        _echo(f"no records at {records}. Run `screener search` first.")
        raise typer.Exit(code=1)

    items: list[Record] = list(read_jsonl(records))
    if limit:
        items = items[:limit]
    if not items:
        _echo("nothing to screen")
        raise typer.Exit(code=1)

    tokens, usd = estimate_cost(spec, items)
    _echo(f"{len(items)} records, ~{tokens:,} input tokens, ~${usd:.4f}")
    if dry_run:
        return

    settings = _settings()
    run = screen_abstracts(
        spec, items, settings=settings, concurrency=concurrency,
        use_cache=not no_cache, on_progress=_progress("screened"),
    )
    run.write_jsonl(out)
    report.write_screening_csv(spec, run, run_dir.screened_csv)

    _echo(f"\n{report.format_counts(run.counts())}")
    _echo(report.format_usage(run.usage))
    _echo(f"-> {out} and {run_dir.screened_csv}")

    failed = [r for r in run.results if r.error]
    if failed:
        _echo(f"\n{len(failed)} records failed and were sent for human review; first: {failed[0].error}")


@app.command()
def fulltext(
    protocol: Path = ProtocolOption,
    screened: Optional[Path] = typer.Option(None, "--screened", "-s"),
    out: Optional[Path] = typer.Option(None, "--out", "-o"),
    limit: Optional[int] = typer.Option(None, "--limit", "-n"),
    concurrency: int = typer.Option(4, "--concurrency", "-c"),
    check_entitlement: bool = typer.Option(
        False, "--check-entitlement", help="Only report how many full texts are obtainable."
    ),
    no_cache: bool = typer.Option(False, "--no-cache"),
) -> None:
    """Stage 2: screen full texts, routing each criterion to the sections it needs."""
    spec = _load(protocol)
    run_dir = paths.run_paths(protocol)
    screened = screened or run_dir.screened
    out = out or run_dir.fulltext
    if not screened.exists():
        _echo(f"no stage-1 output at {screened}. Run `screener screen` first.")
        raise typer.Exit(code=1)

    stage1 = ScreenRun.read_jsonl(screened)
    candidates = shortlist(stage1)
    if limit:
        candidates = candidates[:limit]
    if not candidates:
        _echo("stage 1 left nothing to assess")
        raise typer.Exit(code=1)

    settings = _settings()
    _echo(f"{len(candidates)} records advanced from stage 1")

    if check_entitlement:
        coverage, _ = check_coverage(
            candidates, settings=settings, on_progress=_progress("checked")
        )
        _echo(f"\nEurope PMC coverage: {coverage.retrievable}/{coverage.total} "
              f"({coverage.percentage:.0f}%)")
        for status, count in sorted(coverage.by_status.items(), key=lambda kv: -kv[1]):
            _echo(f"  {status}: {count}")
        if coverage.percentage < 50:
            _echo(
                "\nEurope PMC holds full text for its biomedical open-access subset only, "
                "so a low number here is expected rather than a fault for any topic outside "
                "the life sciences. Everything it cannot supply is reported as PRISMA's "
                "'reports not retrieved'."
            )
        return

    items = fetch_and_parse(
        candidates, settings=settings, use_cache=not no_cache, on_progress=_progress("fetched")
    )
    run = screen_full_texts(
        spec, items, settings=settings, concurrency=concurrency,
        use_cache=not no_cache, on_progress=_progress("assessed"),
    )
    run.write_jsonl(out)
    report.write_fulltext_csv(spec, run, run_dir.fulltext_csv)

    _echo(f"\n{report.format_counts(run.counts())}")
    reasons = run.exclusion_reasons()
    if reasons:
        _echo("exclusion reasons:")
        for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
            _echo(f"  {reason}: {count}")
    _echo(report.format_usage(run.usage, title="Jev usage (stage 2)"))
    _echo(f"-> {out} and {run_dir.fulltext_csv}")


@app.command()
def sections(
    doi: str = typer.Argument(..., help="DOI of an open-access paper held in Europe PMC."),
    chars: int = typer.Option(160, "--chars", help="Preview length per section."),
) -> None:
    """Show how one paper parses into sections. Use this when routing looks wrong."""
    settings = _settings()
    record = Record(id=doi, doi=doi)
    with EuropePMCSource(settings) as source:
        retrieval = source.fetch(record)

    if not retrieval.ok or retrieval.full_text is None:
        _echo(f"could not retrieve {doi}: {retrieval.status.value} — {retrieval.detail or ''}")
        raise typer.Exit(code=1)

    parsed = parse_sections(retrieval.full_text.content)
    _echo(f"title:    {parsed.title[:120]}")
    _echo(f"abstract: {len(parsed.abstract)} chars")
    _echo(f"sections: {len(parsed.sections)}\n")
    for section in parsed.sections:
        canonical = section.canonical.value if section.canonical else "UNMATCHED"
        indent = "  " * section.level
        _echo(f"{indent}[{canonical:<14}] {section.title or '(no heading)'}  ({section.tokens} tok)")
        if section.text:
            _echo(f"{indent}   {section.text[:chars]}…")
    unmatched = len(parsed.unresolved)
    if unmatched:
        _echo(f"\n{unmatched} heading(s) unmatched — stage 2 will spend one Choice labelling them.")


@app.command()
def label(
    protocol: Path = ProtocolOption,
    stage: str = typer.Option("abstracts", "--stage", help="abstracts | fulltext"),
    size: int = typer.Option(50, "--size", "-n"),
    out: Optional[Path] = typer.Option(None, "--out", "-o"),
) -> None:
    """Draw a stratified calibration sample for a human to screen by hand."""
    run_dir = paths.run_paths(protocol)
    if stage == "abstracts":
        source_path, default_out = run_dir.screened, run_dir.labels_stage1
        run: ScreenRun | FullTextRun = ScreenRun.read_jsonl(source_path) if source_path.exists() else ScreenRun()
    elif stage == "fulltext":
        source_path, default_out = run_dir.fulltext, run_dir.labels_stage2
        run = FullTextRun.read_jsonl(source_path) if source_path.exists() else FullTextRun()
    else:
        raise typer.BadParameter("stage must be 'abstracts' or 'fulltext'")

    if not run.results:
        _echo(f"no results at {source_path}")
        raise typer.Exit(code=1)

    target = out or default_out
    picked = stratified_sample(
        [(r.record.id, r.decision.label, r.decision.rank) for r in run.results], size=size
    )
    written = report.write_label_template(run, picked, target)
    _echo(f"wrote {written} records to {target}")
    _echo(
        "Fill in the gold_label column with 'include' or 'exclude', then run "
        f"`screener evaluate --stage {stage}`."
    )
    _echo(
        "The sample is stratified across the decision bands on purpose: a random sample of "
        "an imbalanced corpus would be nearly all obvious excludes and would say nothing "
        "about the error that matters."
    )


@app.command()
def evaluate(
    protocol: Path = ProtocolOption,
    stage: str = typer.Option("abstracts", "--stage", help="abstracts | fulltext"),
    gold: Optional[Path] = typer.Option(None, "--gold", "-g"),
) -> None:
    """Score a stage against the hand-labelled sample."""
    run, gold_path = _run_and_gold(protocol, stage, gold)

    if not gold_path.exists():
        _echo(f"no labels at {gold_path}. Run `screener label --stage {stage}` first.")
        raise typer.Exit(code=1)

    labels = report.read_gold_labels(gold_path)
    metrics = evaluate_metrics(
        ((r.record.id, r.decision.label) for r in run.results), labels
    )
    _echo(format_metrics(metrics, title=f"Stage: {stage}  ({len(labels)} labelled)"))


@app.command()
def tune(
    protocol: Path = ProtocolOption,
    stage: str = typer.Option("abstracts", "--stage"),
    gold: Optional[Path] = typer.Option(None, "--gold", "-g"),
    target_recall: float = typer.Option(0.95, "--target-recall"),
    apply: bool = typer.Option(False, "--apply", help="Write the winning thresholds back to the protocol."),
) -> None:
    """Sweep the thresholds against the labels. Costs nothing — it replays cached answers."""
    spec = _load(protocol)
    run, gold_path = _run_and_gold(protocol, stage, gold)

    if not gold_path.exists():
        _echo(f"no labels at {gold_path}. Run `screener label --stage {stage}` first.")
        raise typer.Exit(code=1)

    labels = report.read_gold_labels(gold_path)
    cases = [(r.record.id, r.answer_values) for r in run.results if r.answers]
    result = sweep(spec, cases, labels, target_recall=target_recall)
    _echo(format_sweep(result))

    if apply and result.best:
        spec.thresholds.include_min = result.best.include_min
        spec.thresholds.exclude_min = result.best.exclude_min
        spec.save(protocol)
        _echo(f"\nwrote thresholds back to {protocol}")


@app.command()
def prisma(
    protocol: Path = ProtocolOption,
    identified: Optional[int] = typer.Option(None, "--identified", help="Records found by the search."),
) -> None:
    """Print the PRISMA 2020 flow counts across both stages."""
    run_dir = paths.run_paths(protocol)
    stage1 = ScreenRun.read_jsonl(run_dir.screened) if run_dir.screened.exists() else None
    stage2 = FullTextRun.read_jsonl(run_dir.fulltext) if run_dir.fulltext.exists() else None
    if stage1 is None and stage2 is None:
        _echo("nothing to report yet — run `screener screen` first.")
        raise typer.Exit(code=1)

    total = identified
    if total is None and run_dir.records.exists():
        total = sum(1 for _ in read_jsonl(run_dir.records))
    flow = build_flow(identified=total or 0, stage1=stage1, stage2=stage2)
    _echo(flow.render())


if __name__ == "__main__":  # pragma: no cover
    app()
