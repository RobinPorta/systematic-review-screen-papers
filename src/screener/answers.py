"""Plain, serialisable versions of Jev's answers.

The SDK returns pydantic models wrapping a live `httpx` response. Normalising them into
these three types the moment a call returns buys two things: the decision logic becomes a
pure function over data that is trivial to construct in a test, and every answer can be
cached to disk as JSON and replayed without spending tokens again.
"""

from __future__ import annotations

from typing import Any, Mapping, Union

from pydantic import BaseModel


class NoulValue(BaseModel):
    kind: str = "noul"
    #: Probability that the answer is yes, 0..1.
    noul: float

    @property
    def uncertainty(self) -> float:
        """How close this sits to a coin flip.

        A Noul carries no `confidence` field — unlike Choice and Score, whose confidence is
        derived from the spread of a distribution. For a single probability the equivalent
        signal is distance from 0.5, which is what this reports (0 = certain, 1 = maximally
        undecided).
        """
        return 1.0 - 2.0 * abs(self.noul - 0.5)


class ChoiceValue(BaseModel):
    kind: str = "choice"
    choice: str
    confidence: float
    probabilities: dict[str, float] = {}


class ScoreValue(BaseModel):
    kind: str = "score"
    score: float
    confidence: float
    probabilities: dict[int, float] = {}
    legend: dict[int, Any] = {}

    def normalised(self, top_level: int) -> float:
        """Put the score on 0..1 so scales of different lengths can be compared.

        A four-level scale returns 0..3 and a three-level scale 0..2, so a top score on one
        is a bigger number than a top score on the other. Dividing by the top level number
        is what makes a weighted combination mean what it says.
        """
        return self.score / top_level if top_level > 0 else 0.0


AnswerValue = Union[NoulValue, ChoiceValue, ScoreValue]


def normalise(answers: Mapping[str, Any]) -> dict[str, AnswerValue]:
    """Convert SDK answer objects into the plain types above.

    Unrecognised answer kinds are skipped rather than raised on: the SDK itself warns and
    skips kinds it predates, and a future question type appearing in a response should not
    take a screening run down.
    """
    out: dict[str, AnswerValue] = {}
    for question_id, answer in answers.items():
        if hasattr(answer, "noul"):
            out[question_id] = NoulValue(noul=float(answer.noul))
        elif hasattr(answer, "choice"):
            out[question_id] = ChoiceValue(
                choice=str(answer.choice),
                confidence=float(getattr(answer, "confidence", 0.0) or 0.0),
                probabilities={str(k): float(v) for k, v in (getattr(answer, "probabilities", {}) or {}).items()},
            )
        elif hasattr(answer, "score"):
            out[question_id] = ScoreValue(
                score=float(answer.score),
                confidence=float(getattr(answer, "confidence", 0.0) or 0.0),
                probabilities={int(k): float(v) for k, v in (getattr(answer, "probabilities", {}) or {}).items()},
                legend={int(k): v for k, v in (getattr(answer, "legend", {}) or {}).items()},
            )
    return out


def to_json(answers: Mapping[str, AnswerValue]) -> dict[str, Any]:
    return {qid: value.model_dump() for qid, value in answers.items()}


def from_json(payload: Mapping[str, Any]) -> dict[str, AnswerValue]:
    out: dict[str, AnswerValue] = {}
    for question_id, raw in payload.items():
        kind = raw.get("kind")
        if kind == "noul":
            out[question_id] = NoulValue.model_validate(raw)
        elif kind == "choice":
            out[question_id] = ChoiceValue.model_validate(raw)
        elif kind == "score":
            out[question_id] = ScoreValue.model_validate(raw)
    return out
