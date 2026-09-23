"""Stage 1: title and abstract screening.

Every question in the protocol goes into a **single** `system_one` call per record. Jev
evaluates the questions in a request in parallel against one ingest of the state, so a call
with sixteen questions costs barely more time than a call with one and only the extra
question tokens — the "speculative fan-out" pattern. Asking a question whose answer only
matters for some records is close to free, and the code simply ignores the rest.

There is no batch endpoint, so records are parallelised client-side with a semaphore.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from pydantic import BaseModel, Field
from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy, TypeSafeError

from .answers import AnswerValue, from_json, normalise, to_json
from .config import CACHE_DIR, Settings
from .decide import Decision, Label, decide
from .protocol import Protocol
from .questions import stage1_questions
from .records import Record

log = logging.getLogger(__name__)

#: Jev 1.13 input pricing. Output tokens are free.
USD_PER_MTOK = 0.042
DEFAULT_CONCURRENCY = 8


class Usage(BaseModel):
    input_tokens: int = 0
    calls: int = 0
    cached_calls: int = 0

    @property
    def usd(self) -> float:
        return self.input_tokens / 1_000_000 * USD_PER_MTOK

    def add(self, tokens: int) -> None:
        self.input_tokens += tokens
        self.calls += 1

    def merge(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.calls += other.calls
        self.cached_calls += other.cached_calls


class ScreenResult(BaseModel):
    record: Record
    decision: Decision
    answers: dict[str, Any] = Field(default_factory=dict)
    input_tokens: int = 0
    cached: bool = False
    error: str | None = None

    @property
    def answer_values(self) -> dict[str, AnswerValue]:
        return from_json(self.answers)


class ScreenRun(BaseModel):
    """Everything one screening pass produced."""

    results: list[ScreenResult] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)

    def by_label(self, label: Label) -> list[ScreenResult]:
        return [r for r in self.results if r.decision.label is label]

    def counts(self) -> dict[str, int]:
        tally: dict[str, int] = {label.value: 0 for label in Label}
        for result in self.results:
            tally[result.decision.label.value] += 1
        return {k: v for k, v in tally.items() if v or k != Label.NOT_RETRIEVABLE.value}

    def write_jsonl(self, path: str | Path) -> int:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for result in self.results:
                handle.write(result.model_dump_json() + "\n")
        return len(self.results)

    @staticmethod
    def read_jsonl(path: str | Path) -> ScreenRun:
        run = ScreenRun()
        with Path(path).open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    result = ScreenResult.model_validate(json.loads(line))
                    run.results.append(result)
                    run.usage.input_tokens += result.input_tokens
                    run.usage.calls += 0 if result.cached else 1
        return run


def questions_fingerprint(questions: dict[str, Any]) -> str:
    """Hash the questions so editing a criterion invalidates the cache for that run."""
    payload = {
        qid: q.model_dump(mode="json") if hasattr(q, "model_dump") else q
        for qid, q in sorted(questions.items())
    }
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def _cache_path(cache_dir: Path, fingerprint: str, record_id: str) -> Path:
    safe = hashlib.sha256(record_id.encode("utf-8")).hexdigest()[:24]
    return cache_dir / fingerprint / f"{safe}.json"


def estimate_tokens(text: str) -> int:
    """A rough token count, used only for cost previews and the stage-2 budget check.

    Four characters per token is the usual English approximation. It is an estimate, which
    is why the stage-2 guard leaves generous headroom rather than trusting this number.
    """
    return max(1, len(text) // 4)


def estimate_cost(protocol: Protocol, records: Sequence[Record]) -> tuple[int, float]:
    """Preview tokens and dollars for a stage-1 run, before committing to it."""
    questions = stage1_questions(protocol)
    question_tokens = estimate_tokens(json.dumps(
        {qid: q.model_dump(mode="json") for qid, q in questions.items()}, default=str
    ))
    total = sum(estimate_tokens(json.dumps(r.state(), default=str)) + question_tokens for r in records)
    return total, total / 1_000_000 * USD_PER_MTOK


async def screen_records(
    protocol: Protocol,
    records: Iterable[Record],
    *,
    settings: Settings,
    concurrency: int = DEFAULT_CONCURRENCY,
    use_cache: bool = True,
    cache_dir: Path | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> ScreenRun:
    """Screen every record, one call each, `concurrency` in flight at a time."""
    records = list(records)
    run = ScreenRun()
    if not records:
        return run

    api_key = settings.require_typesafe()
    questions = stage1_questions(protocol)
    fingerprint = questions_fingerprint(questions)
    cache_root = (cache_dir if cache_dir is not None else CACHE_DIR / "jev")
    semaphore = asyncio.Semaphore(max(1, concurrency))
    done = 0

    async with AsyncTypeSafeClient(
        api_key=api_key,
        model=settings.typesafe_model,
        retry=RetryPolicy(max_retries=4),
    ) as client:

        async def one(record: Record) -> ScreenResult:
            nonlocal done
            cached_path = _cache_path(cache_root, fingerprint, record.id)

            if use_cache and cached_path.exists():
                payload = json.loads(cached_path.read_text(encoding="utf-8"))
                values = from_json(payload["answers"])
                result = ScreenResult(
                    record=record,
                    decision=decide(protocol, values),
                    answers=payload["answers"],
                    input_tokens=payload.get("input_tokens", 0),
                    cached=True,
                )
            else:
                async with semaphore:
                    try:
                        response = await client.system_one(state=record.state(), questions=questions)
                    except TypeSafeError as error:
                        log.warning("screening %s failed: %s", record.id, error)
                        result = ScreenResult(
                            record=record,
                            # A failed call is not evidence either way; a person decides.
                            decision=Decision(label=Label.MAYBE),
                            error=str(error),
                        )
                        done += 1
                        if on_progress:
                            on_progress(done, len(records))
                        return result

                values = normalise(response.answers)
                payload = to_json(values)
                tokens = int(getattr(response.usage, "input_tokens", 0) or 0)
                result = ScreenResult(
                    record=record,
                    decision=decide(protocol, values),
                    answers=payload,
                    input_tokens=tokens,
                )
                if use_cache:
                    cached_path.parent.mkdir(parents=True, exist_ok=True)
                    cached_path.write_text(
                        json.dumps({"answers": payload, "input_tokens": tokens}), encoding="utf-8"
                    )

            done += 1
            if on_progress:
                on_progress(done, len(records))
            return result

        run.results = list(await asyncio.gather(*(one(record) for record in records)))

    for result in run.results:
        run.usage.input_tokens += result.input_tokens
        if result.cached:
            run.usage.cached_calls += 1
        else:
            run.usage.calls += 1
    return run
