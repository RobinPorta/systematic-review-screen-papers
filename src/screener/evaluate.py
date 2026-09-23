"""Measure a screening pass against human labels.

The framing matters more than the arithmetic. In an AI-assisted workflow the screener does
not decide what to include — it decides what a human never has to look at. So the positive
class is "reached a human" (`include` **or** `maybe`), and the only truly costly error is
an eligible study the screener dropped on its own.

That is why recall carries the weight here and why `workload_saved` is reported beside it:
the pair answers "how much reading did this save, and what did it cost me in lost studies".
"""

from __future__ import annotations

from typing import Iterable, Mapping

from pydantic import BaseModel, Field

from .decide import Label

#: Gold labels a human can assign.
GOLD_INCLUDE = "include"
GOLD_EXCLUDE = "exclude"

#: Bands that still reach a reviewer.
RETAINED = {Label.INCLUDE, Label.MAYBE, Label.NOT_RETRIEVABLE}


class Metrics(BaseModel):
    """Screening performance against a labelled sample."""

    n: int = 0
    true_positives: int = 0
    false_negatives: int = 0
    true_negatives: int = 0
    false_positives: int = 0
    #: How gold-eligible records were distributed across the three bands.
    eligible_by_band: dict[str, int] = Field(default_factory=dict)
    unlabelled: int = 0

    @property
    def recall(self) -> float:
        """Of the studies that really are eligible, how many survived screening."""
        denominator = self.true_positives + self.false_negatives
        return self.true_positives / denominator if denominator else 0.0

    @property
    def precision(self) -> float:
        denominator = self.true_positives + self.false_positives
        return self.true_positives / denominator if denominator else 0.0

    @property
    def specificity(self) -> float:
        denominator = self.true_negatives + self.false_positives
        return self.true_negatives / denominator if denominator else 0.0

    @property
    def f1(self) -> float:
        if not (self.precision and self.recall):
            return 0.0
        return 2 * self.precision * self.recall / (self.precision + self.recall)

    @property
    def auto_excluded(self) -> float:
        """Share of records the screener dropped without a human — the raw saving."""
        return (self.true_negatives + self.false_negatives) / self.n if self.n else 0.0

    @property
    def workload_saved(self) -> float:
        """WSS: work saved over sampling, discounted by the recall actually achieved.

        WSS = (TN + FN)/N − (1 − recall). Reading everything scores 0. A screener that
        halves the reading while losing nothing scores 0.5. Losing eligible studies eats
        directly into the number, which is the point — raw reduction alone would reward a
        screener that simply excluded more.
        """
        return self.auto_excluded - (1.0 - self.recall)

    @property
    def missed(self) -> int:
        """Eligible studies the screener dropped. The number to look at first."""
        return self.false_negatives

    def summary(self) -> dict[str, float | int]:
        return {
            "n": self.n,
            "recall": round(self.recall, 4),
            "precision": round(self.precision, 4),
            "specificity": round(self.specificity, 4),
            "f1": round(self.f1, 4),
            "auto_excluded": round(self.auto_excluded, 4),
            "workload_saved": round(self.workload_saved, 4),
            "missed_eligible": self.missed,
        }

    def confusion(self) -> dict[str, int]:
        return {
            "eligible_retained": self.true_positives,
            "eligible_dropped": self.false_negatives,
            "ineligible_dropped": self.true_negatives,
            "ineligible_retained": self.false_positives,
        }


def evaluate(
    predictions: Iterable[tuple[str, Label]],
    gold: Mapping[str, str],
) -> Metrics:
    """Score predictions against gold labels, ignoring records nobody has labelled."""
    metrics = Metrics()
    for record_id, label in predictions:
        truth = (gold.get(record_id) or "").strip().lower()
        if truth not in (GOLD_INCLUDE, GOLD_EXCLUDE):
            metrics.unlabelled += 1
            continue

        metrics.n += 1
        retained = label in RETAINED
        if truth == GOLD_INCLUDE:
            key = label.value
            metrics.eligible_by_band[key] = metrics.eligible_by_band.get(key, 0) + 1
            if retained:
                metrics.true_positives += 1
            else:
                metrics.false_negatives += 1
        else:
            if retained:
                metrics.false_positives += 1
            else:
                metrics.true_negatives += 1
    return metrics


def format_metrics(metrics: Metrics, *, title: str = "Screening performance") -> str:
    """A compact text report, shared by the CLI and the UI."""
    if not metrics.n:
        return (
            f"{title}\n"
            f"  No labelled records. Run `screener label` and fill in the gold_label column."
        )

    lines = [
        title,
        f"  labelled records     {metrics.n}" + (f"  (+{metrics.unlabelled} unlabelled)" if metrics.unlabelled else ""),
        f"  recall               {metrics.recall:6.1%}   <- eligible studies kept",
        f"  precision            {metrics.precision:6.1%}",
        f"  specificity          {metrics.specificity:6.1%}",
        f"  F1                   {metrics.f1:6.1%}",
        f"  auto-excluded        {metrics.auto_excluded:6.1%}   <- reading avoided",
        f"  workload saved (WSS) {metrics.workload_saved:6.1%}",
        "",
        "  confusion matrix",
        f"    eligible, kept       {metrics.true_positives:4d}",
        f"    eligible, DROPPED    {metrics.false_negatives:4d}   <- the error that matters",
        f"    ineligible, kept     {metrics.false_positives:4d}",
        f"    ineligible, dropped  {metrics.true_negatives:4d}",
    ]
    if metrics.eligible_by_band:
        bands = ", ".join(f"{k}={v}" for k, v in sorted(metrics.eligible_by_band.items()))
        lines.append(f"  eligible studies by band: {bands}")
    return "\n".join(lines)
