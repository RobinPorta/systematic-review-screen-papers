"""Sweep the decision thresholds against the labelled sample.

This costs nothing. Every answer from a screening run is stored, so re-deciding the whole
corpus under different thresholds is pure arithmetic over cached probabilities — no calls,
no tokens. That is a direct consequence of Jev returning calibrated numbers rather than a
verdict: the policy can be re-tuned long after the model has stopped talking to you.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Sequence

from pydantic import BaseModel

from .answers import AnswerValue
from .decide import Label, decide
from .evaluate import Metrics, evaluate
from .protocol import Protocol


class SweepPoint(BaseModel):
    include_min: float
    exclude_min: float
    recall: float
    precision: float
    auto_excluded: float
    workload_saved: float
    missed: int
    n: int
    #: Share of records left in the `maybe` band, needing human adjudication.
    needs_human: float = 0.0

    @property
    def label(self) -> str:
        return f"inc≥{self.include_min:.2f} / exc≥{self.exclude_min:.2f}"


def _preference(point: SweepPoint) -> tuple[float, float, float, float]:
    """How to rank two settings that both hold the recall target.

    Order: save the most reading, then keep the most eligible studies, then leave the
    fewest records needing human adjudication, and finally — among settings that are
    otherwise indistinguishable — prefer the **higher** exclusion bar.

    That last term matters more than it looks. `include_min` only moves records between
    `include` and `maybe`, and both still reach a human, so it does not shift recall or
    auto-exclusion at all: whole swathes of the grid tie. Without a tie-break that prefers
    caution, the sweep happily recommends `exc≥0.60` when `exc≥0.99` scored exactly the
    same on your sample — and on a corpus bigger than your sample, the loose one starts
    dropping papers the strict one would have kept.
    """
    return (point.auto_excluded, point.recall, -point.needs_human, point.exclude_min)


class SweepResult(BaseModel):
    points: list[SweepPoint] = []
    target_recall: float = 0.95
    best: SweepPoint | None = None

    def meeting_target(self) -> list[SweepPoint]:
        return [p for p in self.points if p.recall >= self.target_recall]

    def tied_with_best(self) -> list[SweepPoint]:
        """Settings indistinguishable from the winner on this sample.

        A long list here is a warning, not a detail: it means the sample is too small to
        separate these thresholds, and the recommendation rests on the tie-break rather
        than on evidence.
        """
        if self.best is None:
            return []
        best = (self.best.auto_excluded, self.best.recall)
        return [p for p in self.meeting_target() if (p.auto_excluded, p.recall) == best]


def _grid(start: float, stop: float, step: float) -> list[float]:
    values: list[float] = []
    current = start
    while current <= stop + 1e-9:
        values.append(round(current, 3))
        current += step
    return values


def sweep(
    protocol: Protocol,
    cases: Sequence[tuple[str, Mapping[str, AnswerValue]]],
    gold: Mapping[str, str],
    *,
    target_recall: float = 0.95,
    include_grid: Iterable[float] | None = None,
    exclude_grid: Iterable[float] | None = None,
) -> SweepResult:
    """Re-decide every case at each threshold pair and score it.

    The winner is the setting that saves the most reading while still holding the recall
    target. If nothing holds the target, the highest-recall setting is returned instead,
    because at that point the honest answer is "this protocol is not ready to auto-exclude"
    rather than a tuned number.
    """
    includes = list(include_grid) if include_grid is not None else _grid(0.30, 0.90, 0.05)
    excludes = list(exclude_grid) if exclude_grid is not None else _grid(0.60, 0.99, 0.05)

    result = SweepResult(target_recall=target_recall)
    for include_min in includes:
        for exclude_min in excludes:
            tuned = protocol.model_copy(deep=True)
            tuned.thresholds.include_min = include_min
            tuned.thresholds.exclude_min = exclude_min

            predictions = [
                (record_id, decide(tuned, answers).label) for record_id, answers in cases
            ]
            metrics: Metrics = evaluate(predictions, gold)
            if not metrics.n:
                continue
            maybes = sum(1 for _, label in predictions if label is Label.MAYBE)
            result.points.append(
                SweepPoint(
                    include_min=include_min,
                    exclude_min=exclude_min,
                    recall=round(metrics.recall, 4),
                    precision=round(metrics.precision, 4),
                    auto_excluded=round(metrics.auto_excluded, 4),
                    workload_saved=round(metrics.workload_saved, 4),
                    missed=metrics.missed,
                    n=metrics.n,
                    needs_human=round(maybes / len(predictions), 4) if predictions else 0.0,
                )
            )

    qualifying = result.meeting_target()
    if qualifying:
        result.best = max(qualifying, key=_preference)
    elif result.points:
        result.best = max(result.points, key=lambda p: (p.recall, p.auto_excluded))
    return result


def format_sweep(result: SweepResult) -> str:
    if not result.points:
        return "No labelled records to tune against. Run `screener label` first."

    lines = [
        f"Threshold sweep ({len(result.points)} settings, target recall "
        f"{result.target_recall:.0%})",
    ]
    qualifying = result.meeting_target()
    if qualifying:
        lines.append(f"  {len(qualifying)} settings hold the target.")
    else:
        lines.append(
            "  No setting holds the target. Do not auto-exclude yet — sharpen the criteria "
            "wording, or screen this corpus by hand."
        )

    best = result.best
    if best:
        lines += [
            "",
            f"  best: {best.label}",
            f"    recall           {best.recall:6.1%}",
            f"    precision        {best.precision:6.1%}",
            f"    auto-excluded    {best.auto_excluded:6.1%}",
            f"    workload saved   {best.workload_saved:6.1%}",
            f"    needs a human    {best.needs_human:6.1%}",
            f"    eligible missed  {best.missed}",
        ]
        tied = result.tied_with_best()
        if len(tied) > 1:
            bars = sorted({p.exclude_min for p in tied})
            lines += [
                "",
                f"  {len(tied)} settings score identically on this sample "
                f"(exclusion bars {bars[0]:.2f}–{bars[-1]:.2f}).",
                "  The strictest of them is recommended, but the sample cannot tell them",
                "  apart — label more records before trusting the exact number.",
            ]
    return "\n".join(lines)
