"""Turn a `Protocol` into TypeSafe question objects.

Stage 1 puts every question into a single call: Jev answers all questions in a request in
parallel, so extra questions barely change latency and cost only their own tokens. Asking
a question you might not need is close to free — the "speculative fan-out" pattern.

Stage 2 cannot do that, because the state is a whole paper. Instead questions are grouped
into *bundles* by the sections they need, so each call carries a small, focused state.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from typesafe_sdk import Choice, Noul, Score

from .protocol import ChoiceSpec, Criterion, Protocol, ScoreSpec, Section

#: Question id of the synthesised stage-2 Choice that records a PRISMA exclusion reason.
EXCLUSION_REASON_ID = "primary_exclusion_reason"
#: Option used when no exclusion criterion stands out.
NO_SINGLE_REASON = "no_single_reason"
#: Question id of the fallback section classifier.
SECTION_LABEL_ID = "section_label"


def _noul(criterion: Criterion) -> Noul:
    outcomes = criterion.outcomes.as_criteria() if criterion.outcomes else None
    return Noul(instructions=criterion.instructions, criteria=outcomes)


def _choice(spec: ChoiceSpec) -> Choice:
    return Choice(instructions=spec.instructions, criteria=dict(spec.options))


def _score(spec: ScoreSpec) -> Score:
    return Score(instructions=spec.instructions, criteria=list(spec.levels))


def stage1_questions(protocol: Protocol) -> dict[str, object]:
    """Every question the protocol defines, for one call per record."""
    questions: dict[str, object] = {c.id: _noul(c) for c in protocol.criteria}
    questions.update({c.id: _choice(c) for c in protocol.choices})
    questions.update({s.id: _score(s) for s in protocol.scores})
    return questions


def exclusion_reason_question(protocol: Protocol) -> Choice:
    """A Choice over the protocol's exclusion criteria.

    PRISMA 2020 requires full-text exclusions to be reported *with reasons*, and "pick one
    from this fixed list" is precisely what a Choice is for. Asked only on papers the code
    has already decided to exclude, so the answer is a label for a decision already made,
    never the decision itself.
    """
    options: dict[str, object | None] = {c.id: c.label for c in protocol.exclusions}
    options[NO_SINGLE_REASON] = (
        "The study fails eligibility for some other reason, or for several reasons with "
        "no single one standing out."
    )
    return Choice(
        instructions=(
            "This study has been judged ineligible for the review. Which single listed "
            "reason best describes why it does not qualify?"
        ),
        criteria=options,
    )


def section_label_question() -> Choice:
    """Classify a section heading the regex canonicaliser could not place.

    A closed set of a dozen options over a short heading is about the cheapest question
    Jev can be asked, and it is squarely the kind of judgement it is good at.
    """
    return Choice(
        instructions=(
            "Given the heading and opening sentences of a section of a research paper, "
            "which part of the paper is this?"
        ),
        criteria={
            Section.INTRODUCTION.value: "Background, prior work, rationale, aims",
            Section.METHODS.value: "How the study was carried out, in general",
            Section.PARTICIPANTS.value: "Who took part: sample, recruitment, eligibility, setting",
            Section.INTERVENTION.value: "What was done to or by participants: the activity, "
            "programme, materials or procedure",
            Section.MEASURES.value: "What was measured and with which instruments or outcomes",
            Section.ANALYSIS.value: "Statistical or analytical approach",
            Section.RESULTS.value: "What was found: findings, tables, statistics",
            Section.DISCUSSION.value: "Interpretation, implications, comparison with prior work",
            Section.LIMITATIONS.value: "Weaknesses, caveats, threats to validity",
            Section.OTHER.value: "Front or back matter: acknowledgements, funding, ethics, "
            "references, appendices, author contributions",
        },
    )


@dataclass
class Bundle:
    """One stage-2 request: a focused state plus the questions that need it."""

    #: Canonical sections whose text makes up this bundle's state.
    sections: list[Section]
    questions: dict[str, object] = field(default_factory=dict)
    #: Set when a bundle was split to fit the context budget; 0 for an unsplit bundle.
    part: int = 0

    @property
    def key(self) -> str:
        names = "+".join(s.value for s in self.sections)
        return f"{names}#{self.part}" if self.part else names


def _routed(protocol: Protocol) -> list[tuple[str, object, list[Section]]]:
    """Every stage-2 question with the sections it asked to read."""
    routed: list[tuple[str, object, list[Section]]] = []
    for criterion in protocol.criteria:
        routed.append((criterion.id, _noul(criterion), criterion.routed_sections()))
    for choice in protocol.choices:
        routed.append((choice.id, _choice(choice), choice.routed_sections()))
    for score in protocol.scores:
        routed.append((score.id, _score(score), score.routed_sections()))
    return routed


def stage2_bundles(protocol: Protocol) -> list[Bundle]:
    """Group stage-2 questions by the sections they read.

    Questions with identical routing share a call, which for a typical protocol collapses
    ~16 questions into three or four requests. Ordering is stable so cache keys and test
    expectations do not move around.
    """
    grouped: dict[tuple[Section, ...], Bundle] = {}
    for question_id, question, sections in _routed(protocol):
        # Deduplicate while preserving the protocol's ordering.
        ordered: list[Section] = []
        for section in sections:
            if section not in ordered:
                ordered.append(section)
        key = tuple(ordered)
        bundle = grouped.get(key)
        if bundle is None:
            bundle = Bundle(sections=ordered)
            grouped[key] = bundle
        bundle.questions[question_id] = question
    return list(grouped.values())
