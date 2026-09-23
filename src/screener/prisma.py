"""PRISMA 2020 flow counts across both screening stages.

The flow diagram is not decoration — it is how a systematic review reports what happened to
every record, and PRISMA requires full-text exclusions to be given *with reasons*. Those
reasons come from the stage-2 exclusion Choice, which is the whole reason that question
exists.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .decide import Label
from .screen import ScreenRun
from .screen_fulltext import FullTextRun


class PrismaFlow(BaseModel):
    identified: int = 0
    removed_before_screening: int = 0
    screened: int = 0
    excluded_at_screening: int = 0
    sought_for_retrieval: int = 0
    not_retrieved: int = 0
    assessed_for_eligibility: int = 0
    excluded_at_full_text: int = 0
    exclusion_reasons: dict[str, int] = Field(default_factory=dict)
    awaiting_assessment: int = 0
    included: int = 0

    def render(self) -> str:
        lines = [
            "PRISMA 2020 flow",
            f"  Records identified                      {self.identified}",
            f"  Removed before screening                {self.removed_before_screening}",
            f"  Records screened (title/abstract)       {self.screened}",
            f"    excluded                              {self.excluded_at_screening}",
            f"  Reports sought for retrieval            {self.sought_for_retrieval}",
            f"    not retrieved                         {self.not_retrieved}",
            f"  Reports assessed for eligibility        {self.assessed_for_eligibility}",
            f"    excluded, with reasons                {self.excluded_at_full_text}",
        ]
        for reason, count in sorted(self.exclusion_reasons.items(), key=lambda kv: -kv[1]):
            lines.append(f"      {reason}: {count}")
        lines += [
            f"  Awaiting human adjudication             {self.awaiting_assessment}",
            f"  Studies included in review              {self.included}",
        ]
        return "\n".join(lines)


def build_flow(
    *,
    identified: int,
    removed_before_screening: int = 0,
    stage1: ScreenRun | None = None,
    stage2: FullTextRun | None = None,
) -> PrismaFlow:
    """Assemble the flow from both runs.

    A `maybe` at stage 1 is not an exclusion: it goes forward to full text alongside the
    includes, because the screener's job is to decide what a human never has to read, not
    to decide eligibility on its own. `maybe` at stage 2 is counted as awaiting
    adjudication — an honest "a person still has to settle this", which PRISMA has a box
    for and an automated pipeline should never quietly resolve.
    """
    flow = PrismaFlow(identified=identified, removed_before_screening=removed_before_screening)

    if stage1 is not None:
        counts = stage1.counts()
        flow.screened = len(stage1.results)
        flow.excluded_at_screening = counts.get(Label.EXCLUDE.value, 0)
        flow.sought_for_retrieval = flow.screened - flow.excluded_at_screening

    if stage2 is not None:
        counts = stage2.counts()
        flow.not_retrieved = counts.get(Label.NOT_RETRIEVABLE.value, 0)
        flow.assessed_for_eligibility = len(stage2.results) - flow.not_retrieved
        flow.excluded_at_full_text = counts.get(Label.EXCLUDE.value, 0)
        flow.awaiting_assessment = counts.get(Label.MAYBE.value, 0)
        flow.included = counts.get(Label.INCLUDE.value, 0)
        if not flow.sought_for_retrieval:
            flow.sought_for_retrieval = len(stage2.results)
        flow.exclusion_reasons = stage2.exclusion_reasons()
    elif stage1 is not None:
        flow.awaiting_assessment = stage1.counts().get(Label.MAYBE.value, 0)
        flow.included = stage1.counts().get(Label.INCLUDE.value, 0)

    return flow
