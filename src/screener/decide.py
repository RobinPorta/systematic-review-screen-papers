"""Turn Jev's probabilities into a screening decision.

Nothing in this module calls an API. Jev supplies calibrated probabilities; the *policy* —
where the thresholds sit, which way ties break, what gets escalated to a person — is
ordinary Python you can read, test and argue with. That separation is the whole point of
building on a decision model rather than asking a chat model for a verdict.

The thresholds are deliberately asymmetric. Screening is recall-critical: letting an
ineligible paper through costs a reviewer a minute, while dropping an eligible one
corrupts the review. So an exclusion criterion has to fire hard (0.85 by default) to drop
a record, an inclusion criterion needs only a moderate 0.60 to count as met, and anything
that lands between the two goes to a human.
"""

from __future__ import annotations

from enum import Enum
from typing import Mapping

from pydantic import BaseModel, Field

from .answers import AnswerValue, NoulValue, ScoreValue
from .protocol import Protocol
from .questions import EXCLUSION_REASON_ID, NO_SINGLE_REASON

#: Default Score used to rank the "maybe" pile when the protocol names none. See
#: `Protocol.ranking_spec`. Optional — a protocol with no Score at all is fine.
RANKING_SCORE_ID = "topical_fit"


class Label(str, Enum):
    INCLUDE = "include"
    MAYBE = "maybe"
    EXCLUDE = "exclude"
    #: Stage 2 only: eligible at stage 1, but no full text could be obtained.
    NOT_RETRIEVABLE = "not_retrievable"


class Verdict(str, Enum):
    MET = "met"
    NOT_MET = "not_met"
    FIRED = "fired"
    UNCERTAIN = "uncertain"


class Reason(BaseModel):
    """One line of the audit trail behind a decision."""

    criterion_id: str
    label: str
    kind: str
    probability: float
    verdict: Verdict
    #: Stage 2 only: which sections of the paper this judgement actually read.
    sections: list[str] = Field(default_factory=list)

    def describe(self) -> str:
        where = f" (read: {', '.join(self.sections)})" if self.sections else ""
        return f"{self.label}: p={self.probability:.2f} [{self.verdict.value}]{where}"


class Decision(BaseModel):
    label: Label
    reasons: list[Reason] = Field(default_factory=list)
    #: Sort key for the "maybe" pile — higher means more likely relevant.
    rank: float = 0.0
    #: PRISMA exclusion reason (stage 2, excluded papers only).
    exclusion_reason: str | None = None
    exclusion_reason_confidence: float | None = None

    @property
    def deciding_reasons(self) -> list[Reason]:
        """The reasons that actually drove the label, for a compact UI summary."""
        if self.label is Label.EXCLUDE:
            return [r for r in self.reasons if r.verdict is Verdict.FIRED]
        if self.label is Label.MAYBE:
            return [r for r in self.reasons if r.verdict in (Verdict.UNCERTAIN, Verdict.NOT_MET)]
        return [r for r in self.reasons if r.verdict is Verdict.MET]


def _noul(answers: Mapping[str, AnswerValue], question_id: str) -> NoulValue | None:
    value = answers.get(question_id)
    return value if isinstance(value, NoulValue) else None


def _rank(protocol: Protocol, answers: Mapping[str, AnswerValue]) -> float:
    """Rank a record for the reviewer's queue.

    Prefers the protocol's ranking Score, normalised onto 0..1 so it is comparable across
    scales. With no such Score, falls back to the mean of the inclusion probabilities.
    """
    spec = protocol.ranking_spec(RANKING_SCORE_ID)
    value = answers.get(spec.id) if spec is not None else None
    if spec is not None and isinstance(value, ScoreValue):
        return round(value.normalised(spec.top_level), 4)

    probabilities = [
        noul.noul for c in protocol.inclusions if (noul := _noul(answers, c.id)) is not None
    ]
    return round(sum(probabilities) / len(probabilities), 4) if probabilities else 0.0


def decide(
    protocol: Protocol,
    answers: Mapping[str, AnswerValue],
    *,
    sections_read: Mapping[str, list[str]] | None = None,
) -> Decision:
    """Apply the protocol's thresholds to one record's answers.

    `sections_read` is stage-2 only: a map from question id to the sections whose text was
    in the state when that question was asked. It goes into the audit trail so a reviewer
    can see not just what Jev concluded but what it was looking at.
    """
    thresholds = protocol.thresholds
    where = sections_read or {}
    reasons: list[Reason] = []

    def add(criterion, probability: float, verdict: Verdict) -> None:
        reasons.append(
            Reason(
                criterion_id=criterion.id,
                label=criterion.label,
                kind=criterion.kind,
                probability=round(probability, 4),
                verdict=verdict,
                sections=list(where.get(criterion.id, [])),
            )
        )

    # 1. Guards. If the record does not say enough to judge, no threshold below is
    #    meaningful and the only honest answer is "a person should look".
    uninformative = False
    for guard in protocol.guards:
        value = _noul(answers, guard.id)
        if value is None:
            continue
        met = value.noul >= thresholds.informative_min
        add(guard, value.noul, Verdict.MET if met else Verdict.UNCERTAIN)
        if not met:
            uninformative = True

    # 2. Exclusions. Any one firing hard is enough to drop the record.
    fired = False
    for criterion in protocol.exclusions:
        value = _noul(answers, criterion.id)
        if value is None:
            continue
        if value.noul >= thresholds.exclude_min:
            add(criterion, value.noul, Verdict.FIRED)
            fired = True
        else:
            add(criterion, value.noul, Verdict.NOT_MET)

    # 3. Inclusions. Every one must be met.
    all_met = True
    missing = False
    for criterion in protocol.inclusions:
        value = _noul(answers, criterion.id)
        if value is None:
            # A question that came back with no answer is not evidence of eligibility.
            missing = True
            all_met = False
            continue
        met = value.noul >= thresholds.include_min
        add(criterion, value.noul, Verdict.MET if met else Verdict.NOT_MET)
        if not met:
            all_met = False

    rank = _rank(protocol, answers)

    # An exclusion that fires outranks everything: it is the one signal strong enough to
    # act on without a person. A missing answer never produces an automatic verdict.
    if fired and not missing:
        label = Label.EXCLUDE
    elif uninformative or missing:
        label = Label.MAYBE
    elif all_met:
        label = Label.INCLUDE
    else:
        label = Label.MAYBE

    decision = Decision(label=label, reasons=reasons, rank=rank)

    if label is Label.EXCLUDE:
        _attach_exclusion_reason(decision, protocol, answers)
    return decision


def _attach_exclusion_reason(
    decision: Decision, protocol: Protocol, answers: Mapping[str, AnswerValue]
) -> None:
    """Record why a paper was excluded, for the PRISMA flow diagram.

    Prefers Jev's explicit reason Choice when one was asked and came back confident enough.
    Otherwise falls back to the criterion that fired hardest, which is a fact about the
    decision already made rather than a second opinion about it.
    """
    value = answers.get(EXCLUSION_REASON_ID)
    if value is not None and getattr(value, "kind", None) == "choice":
        confident = value.confidence >= protocol.thresholds.reason_confidence_min
        if confident and value.choice != NO_SINGLE_REASON:
            criterion = protocol.criterion(value.choice)
            decision.exclusion_reason = criterion.label if criterion else value.choice
            decision.exclusion_reason_confidence = round(value.confidence, 4)
            return

    fired = [r for r in decision.reasons if r.verdict is Verdict.FIRED]
    if fired:
        strongest = max(fired, key=lambda r: r.probability)
        decision.exclusion_reason = strongest.label
        decision.exclusion_reason_confidence = None
