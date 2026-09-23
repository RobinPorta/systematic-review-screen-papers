"""Stage 2: full-text screening, one call per section bundle.

Stage 1 could put every question in a single call because an abstract is small. A full
paper cannot: Jev's own jaggedness notes name "large state full of irrelevant detail" as a
failure mode, and the remedy they give is to filter in code first and send only what the
question needs.

So stage 2 inverts the shape. Questions are grouped by the sections they read, and each
group gets its own call carrying only those sections. A criterion about who took part sees
the participants section, not the discussion; a criterion about outcomes sees methods and
results. Three or four focused calls per paper, and the audit trail records which sections
each judgement actually had in front of it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from pydantic import BaseModel, Field
from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy, TypeSafeError

from .answers import AnswerValue, ChoiceValue, NoulValue, ScoreValue, from_json, normalise, to_json
from .config import CACHE_DIR, Settings
from .decide import Decision, Label, decide
from .fulltext.base import RetrievalStatus
from .fulltext.sections import ParsedSections
from .protocol import DEFAULT_SECTIONS, Protocol, Section
from .questions import (
    EXCLUSION_REASON_ID,
    SECTION_LABEL_ID,
    Bundle,
    exclusion_reason_question,
    section_label_question,
    stage2_bundles,
)
from .records import Record
from .screen import USD_PER_MTOK, Usage, estimate_tokens, questions_fingerprint

log = logging.getLogger(__name__)

#: Jev allows 32k tokens for state plus the longest question. Stop well short: the token
#: estimate here is a four-characters-per-token approximation, not a real tokeniser, and a
#: 400 halfway through a screening run is a bad way to find out it was optimistic.
MAX_STATE_TOKENS = 22_000
DEFAULT_CONCURRENCY = 4


class SectionSummary(BaseModel):
    title: str
    canonical: str | None
    tokens: int
    #: True when the canonical label came from Jev rather than the regex matcher.
    inferred: bool = False


class FullTextResult(BaseModel):
    record: Record
    status: RetrievalStatus
    decision: Decision
    answers: dict[str, Any] = Field(default_factory=dict)
    #: question id -> the sections whose text was in the state when it was asked.
    sections_read: dict[str, list[str]] = Field(default_factory=dict)
    section_map: list[SectionSummary] = Field(default_factory=list)
    input_tokens: int = 0
    calls: int = 0
    cached: bool = False
    detail: str | None = None

    @property
    def answer_values(self) -> dict[str, AnswerValue]:
        return from_json(self.answers)


class FullTextRun(BaseModel):
    results: list[FullTextResult] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)

    def counts(self) -> dict[str, int]:
        tally: dict[str, int] = {}
        for result in self.results:
            tally[result.decision.label.value] = tally.get(result.decision.label.value, 0) + 1
        return tally

    def exclusion_reasons(self) -> dict[str, int]:
        tally: dict[str, int] = {}
        for result in self.results:
            if result.decision.label is Label.EXCLUDE:
                reason = result.decision.exclusion_reason or "unspecified"
                tally[reason] = tally.get(reason, 0) + 1
        return tally

    def write_jsonl(self, path: str | Path) -> int:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for result in self.results:
                handle.write(result.model_dump_json() + "\n")
        return len(self.results)

    @staticmethod
    def read_jsonl(path: str | Path) -> FullTextRun:
        run = FullTextRun()
        with Path(path).open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    result = FullTextResult.model_validate(json.loads(line))
                    run.results.append(result)
                    run.usage.input_tokens += result.input_tokens
                    run.usage.calls += result.calls
        return run


class Part(BaseModel):
    """One actual request: a bundle's questions over a state small enough to send."""

    bundle_key: str
    state: dict[str, str]
    section_names: list[str]
    tokens: int

    model_config = {"arbitrary_types_allowed": True}


def _anchor(parsed: ParsedSections) -> dict[str, str]:
    """Title and abstract, included in *every* stage-2 state.

    Cheap insurance, and it closes a real hole. Section routing can only send what the
    parser managed to label, and plenty of papers have no heading anyone would call
    "participants". Without an anchor, such a bundle would go out carrying whatever
    fragment happened to match — leaving stage 2 with *less* to go on than stage 1 had
    from the abstract alone, and quietly producing worse answers on more text.

    At a few hundred tokens against a 22k budget, there is no reason not to.
    """
    anchor: dict[str, str] = {}
    if parsed.title:
        anchor["title"] = parsed.title
    if parsed.abstract:
        anchor["abstract"] = parsed.abstract
    return anchor


def _best_effort_body(parsed: ParsedSections, budget: int) -> dict[str, str]:
    """Body text to fall back on when none of a bundle's requested sections exist.

    Papers whose headings resist labelling still have a body, and discarding it would mean
    screening a full-text stage on the abstract. Sections are taken in document order,
    skipping front and back matter, until the budget runs out.
    """
    collected: list[str] = []
    used = 0
    for section in parsed.sections:
        if not section.text or section.canonical is Section.OTHER:
            continue
        tokens = section.tokens
        if used + tokens > budget:
            continue
        heading = section.title.strip()
        collected.append(f"{heading}\n{section.text}" if heading else section.text)
        used += tokens
    return {"body": "\n\n".join(collected)} if collected else {}


_TRUNCATION_MARKER = " […section truncated to fit the context budget…]"


def _truncate(text: str, token_budget: int) -> str:
    """Trim a single oversized section, marking the cut so it is visible in the state.

    The marker is charged against the budget rather than appended after it — otherwise the
    very act of noting the truncation pushes the state back over the limit it was trimmed
    to fit.
    """
    char_budget = token_budget * 4
    if len(text) <= char_budget:
        return text
    return text[: char_budget - len(_TRUNCATION_MARKER)].rsplit(" ", 1)[0] + _TRUNCATION_MARKER


def build_parts(bundle: Bundle, parsed: ParsedSections, *, budget: int = MAX_STATE_TOKENS) -> list[Part]:
    """Turn one bundle into one or more requests that fit the context budget.

    Almost always returns a single part. When a bundle's sections together exceed the
    budget it splits on section boundaries first, and only truncates when a single section
    is itself too long — so the split follows the paper's own structure rather than
    chopping mid-argument.

    Every state carries the title and abstract, plus the routed sections; if none of those
    sections exist in this paper, whatever body text there is stands in for them. Between
    them those two rules guarantee a stage-2 question never sees less than stage 1 did.
    """
    routed = parsed.text_for(bundle.sections)
    has_body = any(name not in ("title", "abstract") for name in routed)

    state = _anchor(parsed)
    if has_body:
        state.update(routed)
    else:
        remaining = budget - sum(estimate_tokens(text) for text in state.values())
        state.update(_best_effort_body(parsed, max(remaining, 0)))

    if not state:
        # Nothing parsed at all — no request to make, and the caller reports it as such.
        return []

    total = sum(estimate_tokens(text) for text in state.values())
    if total <= budget:
        return [
            Part(
                bundle_key=bundle.key,
                state=state,
                section_names=list(state.keys()),
                tokens=total,
            )
        ]

    parts: list[Part] = []
    current: dict[str, str] = {}
    current_tokens = 0
    for name, text in state.items():
        tokens = estimate_tokens(text)
        if tokens > budget:
            if current:
                parts.append(Part(
                    bundle_key=bundle.key, state=current,
                    section_names=list(current), tokens=current_tokens,
                ))
                current, current_tokens = {}, 0
            trimmed = _truncate(text, budget)
            parts.append(Part(
                bundle_key=bundle.key, state={name: trimmed},
                section_names=[name], tokens=estimate_tokens(trimmed),
            ))
            continue
        if current_tokens + tokens > budget and current:
            parts.append(Part(
                bundle_key=bundle.key, state=current,
                section_names=list(current), tokens=current_tokens,
            ))
            current, current_tokens = {}, 0
        current[name] = text
        current_tokens += tokens

    if current:
        parts.append(Part(
            bundle_key=bundle.key, state=current,
            section_names=list(current), tokens=current_tokens,
        ))

    for index, part in enumerate(parts, start=1):
        part.bundle_key = f"{bundle.key}#{index}"
    return parts


def merge_answers(
    collected: dict[str, list[AnswerValue]],
) -> dict[str, AnswerValue]:
    """Combine answers when a bundle was split across several parts.

    A Noul takes the maximum: these criteria ask whether the paper *contains* some
    evidence, so a yes from any part of the text is a yes. A Choice or Score takes the
    answer the model was most confident about, since averaging distributions across
    disjoint slices of a paper would invent a certainty none of them had.
    """
    merged: dict[str, AnswerValue] = {}
    for question_id, values in collected.items():
        if not values:
            continue
        if len(values) == 1:
            merged[question_id] = values[0]
            continue
        first = values[0]
        if isinstance(first, NoulValue):
            merged[question_id] = max(values, key=lambda v: v.noul)  # type: ignore[union-attr]
        elif isinstance(first, (ChoiceValue, ScoreValue)):
            merged[question_id] = max(values, key=lambda v: getattr(v, "confidence", 0.0))
        else:
            merged[question_id] = first
    return merged


async def resolve_section_labels(
    client: AsyncTypeSafeClient, parsed: ParsedSections, *, max_sections: int = 12
) -> int:
    """Label sections the regex canonicaliser could not place.

    One Choice over a dozen options, given a heading and its opening sentences — about the
    cheapest question Jev can be asked, and a closed-set classification is exactly what it
    is good at. Only the leftovers get here; well-behaved headings never cost a call.
    """
    unresolved = [s for s in parsed.unresolved if s.title or s.text][:max_sections]
    if not unresolved:
        return 0

    state = {
        f"section_{i}": {"heading": section.title or "(no heading)", "opening": section.text[:200]}
        for i, section in enumerate(unresolved)
    }
    question = section_label_question()
    questions = {
        f"{SECTION_LABEL_ID}_{i}": question.model_copy(
            update={"instructions": f"{question.instructions} Judge `section_{i}`."}
        )
        for i in range(len(unresolved))
    }

    try:
        response = await client.system_one(state=state, questions=questions)
    except TypeSafeError as error:
        log.warning("section labelling failed: %s", error)
        return 0

    values = normalise(response.answers)
    for index, section in enumerate(unresolved):
        value = values.get(f"{SECTION_LABEL_ID}_{index}")
        if isinstance(value, ChoiceValue):
            try:
                section.canonical = Section(value.choice)
            except ValueError:
                section.canonical = Section.OTHER
    return int(getattr(response.usage, "input_tokens", 0) or 0)


def _section_map(parsed: ParsedSections, inferred_before: set[int]) -> list[SectionSummary]:
    return [
        SectionSummary(
            title=section.title or "(no heading)",
            canonical=section.canonical.value if section.canonical else None,
            tokens=section.tokens,
            inferred=index in inferred_before and section.canonical is not None,
        )
        for index, section in enumerate(parsed.sections)
    ]


def _stage2_questions(protocol: Protocol) -> list[Bundle]:
    """Bundles, plus the PRISMA exclusion-reason Choice attached to the default bundle.

    The reason question is asked on every paper, not only the excluded ones. That is the
    speculative fan-out pattern: one extra question inside an existing call costs a few
    tokens and no extra round trip, where asking it conditionally would cost a whole second
    pass over the papers that need it. `decide` reads the answer only when the label is
    EXCLUDE and ignores it otherwise.
    """
    bundles = stage2_bundles(protocol)
    default = tuple(DEFAULT_SECTIONS)
    target = next((b for b in bundles if tuple(b.sections) == default), None)
    if target is None:
        target = Bundle(sections=list(DEFAULT_SECTIONS))
        bundles.append(target)
    target.questions[EXCLUSION_REASON_ID] = exclusion_reason_question(protocol)
    return bundles


async def screen_fulltexts(
    protocol: Protocol,
    items: Sequence[tuple[Record, ParsedSections, RetrievalStatus]],
    *,
    settings: Settings,
    concurrency: int = DEFAULT_CONCURRENCY,
    use_cache: bool = True,
    cache_dir: Path | None = None,
    label_unknown_sections: bool = True,
    on_progress: Callable[[int, int], None] | None = None,
) -> FullTextRun:
    """Screen each retrieved paper across its section bundles.

    Records whose full text could not be obtained still appear in the run, labelled
    NOT_RETRIEVABLE — that is a box on the PRISMA diagram, not an error to swallow.
    """
    run = FullTextRun()
    if not items:
        return run

    api_key = settings.require_typesafe()
    bundles = _stage2_questions(protocol)
    fingerprint = questions_fingerprint(
        {f"{b.key}:{qid}": q for b in bundles for qid, q in b.questions.items()}
    )
    cache_root = (cache_dir if cache_dir is not None else CACHE_DIR / "jev") / "fulltext" / fingerprint
    semaphore = asyncio.Semaphore(max(1, concurrency))
    done = 0

    async with AsyncTypeSafeClient(
        api_key=api_key,
        model=settings.typesafe_model,
        retry=RetryPolicy(max_retries=4),
    ) as client:

        async def one(record: Record, parsed: ParsedSections, status: RetrievalStatus) -> FullTextResult:
            nonlocal done
            try:
                if status is not RetrievalStatus.OK:
                    return FullTextResult(
                        record=record,
                        status=status,
                        decision=Decision(label=Label.NOT_RETRIEVABLE),
                        detail=f"full text not retrieved ({status.value})",
                    )

                cache_file = cache_root / f"{abs(hash(record.id)):x}.json"
                if use_cache and cache_file.exists():
                    payload = json.loads(cache_file.read_text(encoding="utf-8"))
                    values = from_json(payload["answers"])
                    return FullTextResult(
                        record=record,
                        status=status,
                        decision=decide(protocol, values, sections_read=payload["sections_read"]),
                        answers=payload["answers"],
                        sections_read=payload["sections_read"],
                        section_map=[SectionSummary.model_validate(s) for s in payload.get("section_map", [])],
                        input_tokens=payload.get("input_tokens", 0),
                        calls=payload.get("calls", 0),
                        cached=True,
                    )

                tokens = 0
                calls = 0
                unresolved_before = {i for i, s in enumerate(parsed.sections) if s.canonical is None}
                if label_unknown_sections and unresolved_before:
                    async with semaphore:
                        labelled_tokens = await resolve_section_labels(client, parsed)
                    if labelled_tokens:
                        tokens += labelled_tokens
                        calls += 1

                collected: dict[str, list[AnswerValue]] = {}
                sections_read: dict[str, list[str]] = {}

                for bundle in bundles:
                    for part in build_parts(bundle, parsed):
                        async with semaphore:
                            try:
                                response = await client.system_one(
                                    state=part.state, questions=bundle.questions
                                )
                            except TypeSafeError as error:
                                log.warning("stage 2 %s/%s failed: %s", record.id, part.bundle_key, error)
                                continue
                        calls += 1
                        tokens += int(getattr(response.usage, "input_tokens", 0) or 0)
                        for question_id, value in normalise(response.answers).items():
                            collected.setdefault(question_id, []).append(value)
                            for name in part.section_names:
                                if name not in sections_read.setdefault(question_id, []):
                                    sections_read[question_id].append(name)

                values = merge_answers(collected)
                if not values:
                    return FullTextResult(
                        record=record,
                        status=status,
                        decision=Decision(label=Label.MAYBE),
                        section_map=_section_map(parsed, unresolved_before),
                        input_tokens=tokens,
                        calls=calls,
                        detail="every stage-2 call failed; sent for human assessment",
                    )

                result = FullTextResult(
                    record=record,
                    status=status,
                    decision=decide(protocol, values, sections_read=sections_read),
                    answers=to_json(values),
                    sections_read=sections_read,
                    section_map=_section_map(parsed, unresolved_before),
                    input_tokens=tokens,
                    calls=calls,
                )
                if use_cache:
                    cache_file.parent.mkdir(parents=True, exist_ok=True)
                    cache_file.write_text(json.dumps({
                        "answers": result.answers,
                        "sections_read": sections_read,
                        "section_map": [s.model_dump() for s in result.section_map],
                        "input_tokens": tokens,
                        "calls": calls,
                    }), encoding="utf-8")
                return result
            finally:
                done += 1
                if on_progress:
                    on_progress(done, len(items))

        run.results = list(await asyncio.gather(*(one(r, p, s) for r, p, s in items)))

    for result in run.results:
        run.usage.input_tokens += result.input_tokens
        if result.cached:
            run.usage.cached_calls += result.calls
        else:
            run.usage.calls += result.calls
    return run


def estimate_fulltext_cost(
    protocol: Protocol, parsed_papers: Iterable[ParsedSections]
) -> tuple[int, float]:
    """Preview tokens and dollars for a stage-2 run."""
    bundles = _stage2_questions(protocol)
    question_tokens = sum(
        estimate_tokens(json.dumps(
            {qid: q.model_dump(mode="json") for qid, q in bundle.questions.items()}, default=str
        ))
        for bundle in bundles
    )
    total = 0
    for parsed in parsed_papers:
        for bundle in bundles:
            total += sum(part.tokens for part in build_parts(bundle, parsed))
        total += question_tokens
    return total, total / 1_000_000 * USD_PER_MTOK
