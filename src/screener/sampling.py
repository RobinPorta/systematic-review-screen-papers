"""Draw the calibration sample a human will screen by hand.

Sampling at random would be a mistake. Screening corpora are wildly imbalanced — a few
percent eligible at best — so a random fifty records would be almost all obvious excludes
and would tell you nothing about the error that matters, which is an eligible study landing
in the `exclude` band. Stratifying across the three bands guarantees the sample contains
the cases the recall estimate actually depends on.
"""

from __future__ import annotations

import random
from typing import Sequence

from .decide import Label

#: How the sample is split across bands. `exclude` carries the most weight because that is
#: the only band where an error is invisible without checking.
DEFAULT_WEIGHTS: dict[Label, float] = {
    Label.EXCLUDE: 0.45,
    Label.MAYBE: 0.35,
    Label.INCLUDE: 0.20,
}


def stratified_sample(
    items: Sequence[tuple[str, Label, float]],
    *,
    size: int = 50,
    seed: int = 20260918,
    weights: dict[Label, float] | None = None,
) -> list[str]:
    """Pick `size` record ids spread across the decision bands.

    `items` is (record_id, label, rank). Within the `maybe` band the highest-ranked records
    are taken first — those are the borderline cases a reviewer most needs to adjudicate —
    while `include` and `exclude` are sampled at random so the estimate stays unbiased.
    """
    weights = weights or DEFAULT_WEIGHTS
    rng = random.Random(seed)

    by_band: dict[Label, list[tuple[str, float]]] = {label: [] for label in weights}
    for record_id, label, rank in items:
        if label in by_band:
            by_band[label].append((record_id, rank))

    picked: list[str] = []
    shortfall = 0
    for label, weight in weights.items():
        pool = by_band.get(label, [])
        want = int(round(size * weight)) + shortfall
        if not pool:
            shortfall = want
            continue
        if label is Label.MAYBE:
            pool = sorted(pool, key=lambda pair: pair[1], reverse=True)
            chosen = [record_id for record_id, _ in pool[:want]]
        else:
            chosen = [record_id for record_id, _ in rng.sample(pool, min(want, len(pool)))]
        shortfall = max(0, want - len(chosen))
        picked.extend(chosen)

    # Any shortfall left over is topped up from whatever is unused, so the sample still
    # reaches the requested size on a corpus with an empty band.
    if len(picked) < size:
        taken = set(picked)
        leftovers = [record_id for record_id, _, _ in items if record_id not in taken]
        rng.shuffle(leftovers)
        picked.extend(leftovers[: size - len(picked)])

    return picked[:size]
