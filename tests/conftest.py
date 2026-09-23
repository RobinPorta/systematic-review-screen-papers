from __future__ import annotations

from pathlib import Path

import pytest

from screener.answers import ChoiceValue, NoulValue, ScoreValue
from screener.protocol import Protocol

FIXTURES = Path(__file__).parent / "fixtures"

#: A miniature protocol: two inclusions, two exclusions, one guard, one ranking Score.
#: Small enough to reason about by hand, which is the point of testing policy separately
#: from the model that feeds it.
TINY = {
    "name": "tiny",
    "query": '"board game" OR chess',
    "filters": {"year_from": 2000, "languages": ["en"], "types": ["article"]},
    "thresholds": {"include_min": 0.6, "exclude_min": 0.85, "informative_min": 0.4},
    "criteria": [
        {
            "id": "informative",
            "kind": "guard",
            "label": "Says enough to judge",
            "instructions": "The record says enough to judge eligibility.",
            "sections": ["title", "abstract"],
        },
        {
            "id": "inc_humans",
            "kind": "inclusion",
            "label": "Human participants",
            "instructions": "The study collected data from human participants.",
            "sections": ["methods", "participants"],
        },
        {
            "id": "inc_played",
            "kind": "inclusion",
            "label": "Played a physical game",
            "instructions": "Participants played a physical game.",
            "sections": ["methods", "intervention"],
        },
        {
            "id": "exc_digital",
            "kind": "exclusion",
            "label": "Digital games only",
            "instructions": "Every game involved is digital.",
            "sections": ["methods", "intervention"],
        },
        {
            "id": "exc_gambling",
            "kind": "exclusion",
            "label": "Gambling context",
            "instructions": "The study is about gambling.",
            "sections": ["methods", "intervention"],
        },
    ],
    "choices": [
        {
            "id": "design",
            "instructions": "Which design?",
            "options": {"rct": "Randomised", "other": "Anything else"},
            "sections": ["methods", "participants"],
        }
    ],
    "scores": [
        {
            "id": "topical_fit",
            "instructions": "How central is game play?",
            "levels": ["incidental", "partial", "central"],
            "sections": ["title", "abstract"],
        }
    ],
}


@pytest.fixture
def protocol() -> Protocol:
    return Protocol.model_validate(TINY)


@pytest.fixture
def real_protocol() -> Protocol:
    return Protocol.load(Path(__file__).parents[1] / "protocols" / "boardgames.yaml")


def answers(
    *,
    informative: float = 0.9,
    inc_humans: float = 0.9,
    inc_played: float = 0.9,
    exc_digital: float = 0.05,
    exc_gambling: float = 0.02,
    topical_fit: float = 2.0,
    **extra: object,
) -> dict[str, object]:
    """Build a full answer set, overriding only what a test cares about."""
    values: dict[str, object] = {
        "informative": NoulValue(noul=informative),
        "inc_humans": NoulValue(noul=inc_humans),
        "inc_played": NoulValue(noul=inc_played),
        "exc_digital": NoulValue(noul=exc_digital),
        "exc_gambling": NoulValue(noul=exc_gambling),
        "design": ChoiceValue(choice="rct", confidence=0.8, probabilities={"rct": 0.9, "other": 0.1}),
        "topical_fit": ScoreValue(
            score=topical_fit, confidence=0.9, probabilities={2: 1.0}, legend={}
        ),
    }
    values.update(extra)
    return values
