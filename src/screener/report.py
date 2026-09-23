"""CSV output, gold-label files, and the run summary.

The screening CSV is the artefact a reviewer actually works from, so it carries the
probabilities and the reasons alongside the label. A screener that says "exclude" without
saying which criterion fired and how strongly is not auditable, and an unauditable
screening decision has no place in a systematic review.
"""

from __future__ import annotations

import csv
import io
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Iterable, Iterator, Mapping

from .answers import NoulValue
from .decide import Label
from .protocol import Protocol
from .screen import ScreenRun, Usage
from .screen_fulltext import FullTextRun

GOLD_COLUMN = "gold_label"

_BASE_COLUMNS = [
    "record_id",
    "label",
    "rank",
    "year",
    "title",
    "journal",
    "doi",
    "exclusion_reason",
    "deciding_reasons",
]


#: A file path, or an open text handle such as `io.StringIO` for building a download.
Target = str | Path | IO[str]


@contextmanager
def _opened(target: Target) -> Iterator[IO[str]]:
    if hasattr(target, "write"):
        yield target
        return
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        yield handle


def as_text(write, *args) -> str:
    """Run a `write_*` function into memory and return the CSV text."""
    buffer = io.StringIO()
    write(*args, buffer)
    return buffer.getvalue()


def write_screening_csv(protocol: Protocol, run: ScreenRun, path: Target) -> int:
    """One row per record: the decision, the audit trail, and every criterion probability."""

    criterion_ids = [c.id for c in protocol.criteria]
    columns = _BASE_COLUMNS + [f"p_{cid}" for cid in criterion_ids]

    with _opened(path) as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for result in run.results:
            values = result.answer_values
            row = {
                "record_id": result.record.id,
                "label": result.decision.label.value,
                "rank": f"{result.decision.rank:.4f}",
                "year": result.record.year or "",
                "title": result.record.title,
                "journal": result.record.journal,
                "doi": result.record.doi or "",
                "exclusion_reason": result.decision.exclusion_reason or "",
                "deciding_reasons": "; ".join(r.describe() for r in result.decision.deciding_reasons),
            }
            for cid in criterion_ids:
                value = values.get(cid)
                row[f"p_{cid}"] = f"{value.noul:.4f}" if isinstance(value, NoulValue) else ""
            writer.writerow(row)
    return len(run.results)


def write_fulltext_csv(protocol: Protocol, run: FullTextRun, path: Target) -> int:
    """One row per assessed paper, including which sections each judgement read."""

    criterion_ids = [c.id for c in protocol.criteria]
    columns = _BASE_COLUMNS + ["retrieval_status", "sections_parsed", "calls"] + [
        f"p_{cid}" for cid in criterion_ids
    ]

    with _opened(path) as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for result in run.results:
            values = result.answer_values
            row = {
                "record_id": result.record.id,
                "label": result.decision.label.value,
                "rank": f"{result.decision.rank:.4f}",
                "year": result.record.year or "",
                "title": result.record.title,
                "journal": result.record.journal,
                "doi": result.record.doi or "",
                "exclusion_reason": result.decision.exclusion_reason or "",
                "deciding_reasons": "; ".join(r.describe() for r in result.decision.deciding_reasons),
                "retrieval_status": result.status.value,
                "sections_parsed": len(result.section_map),
                "calls": result.calls,
            }
            for cid in criterion_ids:
                value = values.get(cid)
                row[f"p_{cid}"] = f"{value.noul:.4f}" if isinstance(value, NoulValue) else ""
            writer.writerow(row)
    return len(run.results)


def write_label_template(
    run: ScreenRun | FullTextRun,
    record_ids: Iterable[str],
    path: Target,
) -> int:
    """Write the calibration sample with an empty `gold_label` column for a human to fill.

    Column order puts title and abstract next to the blank so the file is genuinely
    screenable in a spreadsheet, which is where this work actually gets done.
    """

    wanted = list(record_ids)
    order = {record_id: index for index, record_id in enumerate(wanted)}
    chosen = [r for r in run.results if r.record.id in order]
    chosen.sort(key=lambda r: order[r.record.id])

    with _opened(path) as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "record_id", GOLD_COLUMN, "predicted", "title", "abstract", "year", "journal", "doi",
        ])
        for result in chosen:
            writer.writerow([
                result.record.id,
                "",
                result.decision.label.value,
                result.record.title,
                result.record.abstract,
                result.record.year or "",
                result.record.journal,
                result.record.doi or "",
            ])
    return len(chosen)


def read_gold_labels(path: str | Path) -> dict[str, str]:
    """Read a filled-in label file. Blank rows are simply not yet labelled."""
    with Path(path).open(encoding="utf-8", newline="") as handle:
        return gold_labels(csv.DictReader(handle))


def gold_labels(rows: Iterable[Mapping[str, object]]) -> dict[str, str]:
    """record id -> gold label, from label-file rows. Blank rows are not yet labelled."""
    labels: dict[str, str] = {}
    for row in rows:
        record_id = str(row.get("record_id") or "").strip()
        value = str(row.get(GOLD_COLUMN) or "").strip().lower()
        if record_id and value:
            labels[record_id] = value
    return labels


def format_usage(usage: Usage, *, title: str = "Jev usage") -> str:
    """The cost line. Worth printing every run — the numbers are the surprise."""
    per_call = usage.input_tokens / usage.calls if usage.calls else 0
    cached = f", {usage.cached_calls} served from cache" if usage.cached_calls else ""
    return (
        f"{title}: {usage.calls} calls{cached}, {usage.input_tokens:,} input tokens "
        f"({per_call:,.0f}/call), ${usage.usd:.4f}"
    )


def format_counts(counts: Mapping[str, int]) -> str:
    total = sum(counts.values())
    order = [Label.INCLUDE.value, Label.MAYBE.value, Label.EXCLUDE.value, Label.NOT_RETRIEVABLE.value]
    parts = [f"{name}={counts[name]}" for name in order if name in counts]
    for name, value in counts.items():
        if name not in order:
            parts.append(f"{name}={value}")
    return f"{total} records: " + ", ".join(parts)
