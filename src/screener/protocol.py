"""The review protocol: criteria, filters, thresholds, and stage-2 section routing.

The protocol lives in YAML rather than in code so criteria wording can be iterated on
without touching Python — which matters, because with Jev the wording *is* the logic.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator


class Section(str, Enum):
    """Canonical section names used to route stage-2 questions.

    Real papers use hundreds of section headings; `fulltext.sections` maps them onto
    this small closed set so the routing table in the protocol stays readable.
    """

    TITLE = "title"
    ABSTRACT = "abstract"
    INTRODUCTION = "introduction"
    METHODS = "methods"
    PARTICIPANTS = "participants"
    INTERVENTION = "intervention"
    MEASURES = "measures"
    ANALYSIS = "analysis"
    RESULTS = "results"
    DISCUSSION = "discussion"
    LIMITATIONS = "limitations"
    OTHER = "other"


#: Question ids become keys in the Jev request and column names in the CSVs.
QuestionId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_]*$")]
NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

#: Sections used when a question's routing list is empty.
DEFAULT_SECTIONS = [Section.TITLE, Section.ABSTRACT, Section.METHODS]


class OutcomeDescriptions(BaseModel):
    """The `criteria` of a Noul question: what a yes and a no each mean.

    Optional, but the documented fix for Jev reading an instruction literally when the
    yes/no boundary is subtle. Either side may itself be structured (a dict or list).
    """

    true_: object | None = Field(default=None, alias="true")
    false_: object | None = Field(default=None, alias="false")

    model_config = {"populate_by_name": True}

    def as_criteria(self) -> dict[str, object] | None:
        if self.true_ is None and self.false_ is None:
            return None
        return {"true": self.true_, "false": self.false_}


class Criterion(BaseModel):
    """One yes/no eligibility judgement, asked as a Noul."""

    id: QuestionId
    kind: Literal["inclusion", "exclusion", "guard"]
    #: Short human-readable name. Used verbatim as a PRISMA exclusion reason.
    label: NonEmpty
    instructions: NonEmpty
    outcomes: OutcomeDescriptions | None = None
    #: Stage-2 routing. Empty means DEFAULT_SECTIONS.
    sections: list[Section] = Field(default_factory=list)

    def routed_sections(self) -> list[Section]:
        return self.sections or list(DEFAULT_SECTIONS)


class ChoiceSpec(BaseModel):
    """A closed-set classification, asked as a Choice.

    Not eligibility logic — these describe the record for reviewers and for the
    "every age of life" breakdown.
    """

    id: QuestionId
    instructions: NonEmpty
    #: option name -> description. A null description is fine when the name speaks for itself.
    options: dict[str, object | None]
    sections: list[Section] = Field(default_factory=list)

    @field_validator("options")
    @classmethod
    def _at_least_two(cls, v: dict[str, object | None]) -> dict[str, object | None]:
        if len(v) < 2:
            raise ValueError("a Choice needs at least two options")
        if len(v) > 255:
            raise ValueError("Jev accepts at most 255 Choice options")
        return v

    def routed_sections(self) -> list[Section]:
        return self.sections or list(DEFAULT_SECTIONS)


class ScoreSpec(BaseModel):
    """An ordered rating, asked as a Score. Levels run low to high."""

    id: QuestionId
    instructions: NonEmpty
    levels: list[object]
    sections: list[Section] = Field(default_factory=list)

    @field_validator("levels")
    @classmethod
    def _two_to_ten(cls, v: list[object]) -> list[object]:
        if not 2 <= len(v) <= 10:
            raise ValueError("a Score needs between 2 and 10 levels")
        return v

    @property
    def top_level(self) -> int:
        """The highest level number, for normalising a score onto 0..1."""
        return len(self.levels) - 1

    def routed_sections(self) -> list[Section]:
        return self.sections or list(DEFAULT_SECTIONS)


class Filters(BaseModel):
    """Filters applied in the OpenAlex query and re-checked in code — never asked of Jev.

    Jev's documented weak spots include counting and date comparison, so years, languages
    and work types are handled by the search index and by ordinary Python.
    """

    year_from: int | None = None
    year_to: int | None = None
    #: ISO 639-1 codes, e.g. ["en", "it"].
    languages: list[str] = Field(default_factory=list)
    #: OpenAlex work types to keep, e.g. article, review, conference-paper, book-chapter.
    types: list[str] = Field(default_factory=list)
    #: Drop records with no abstract at the source.
    #:
    #: Off by default, and that default is a methodological position rather than a
    #: performance one. A record with no abstract is not evidence of ineligibility, and
    #: PRISMA expects it to be screened on its title or sought as full text — not silently
    #: discarded. Left off, such records reach the `abstract_informative` guard, fail it,
    #: and land in `maybe` for a human. Turning it on trades a little recall for a smaller
    #: corpus; say so in your review's methods if you do.
    require_abstract: bool = False

    @model_validator(mode="after")
    def _ordered(self) -> Filters:
        if self.year_from and self.year_to and self.year_from > self.year_to:
            raise ValueError(f"year_from {self.year_from} is after year_to {self.year_to}")
        return self


class Thresholds(BaseModel):
    """Where the three decision bands fall.

    Deliberately asymmetric: screening is recall-critical, so a criterion has to fire
    hard before a record is dropped, while anything ambiguous goes to a human.
    """

    #: An inclusion criterion must reach this to count as met.
    include_min: float = 0.60
    #: An exclusion criterion must reach this to drop the record outright.
    exclude_min: float = 0.85
    #: Below this, the abstract does not say enough to judge — send to a human.
    informative_min: float = 0.40
    #: Confidence floor for the stage-2 exclusion-reason Choice.
    reason_confidence_min: float = 0.30

    @model_validator(mode="after")
    def _in_range(self) -> Thresholds:
        for name in ("include_min", "exclude_min", "informative_min", "reason_confidence_min"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1, got {value}")
        return self


class Protocol(BaseModel):
    """A complete review protocol."""

    name: str
    description: str = ""
    #: The topical part of the search. Filters are appended by `query.build_filter`.
    query: str
    filters: Filters = Field(default_factory=Filters)
    thresholds: Thresholds = Field(default_factory=Thresholds)
    criteria: list[Criterion] = Field(default_factory=list)
    choices: list[ChoiceSpec] = Field(default_factory=list)
    scores: list[ScoreSpec] = Field(default_factory=list)
    #: Id of the Score that ranks the reviewer's queue. Unset means `topical_fit` if the
    #: protocol has one, else the first Score, else the mean inclusion probability.
    ranking_score: str | None = None

    @model_validator(mode="after")
    def _unique_ids_and_one_guard(self) -> Protocol:
        ids = [c.id for c in self.criteria] + [c.id for c in self.choices] + [s.id for s in self.scores]
        duplicates = {i for i in ids if ids.count(i) > 1}
        if duplicates:
            raise ValueError(f"duplicate question ids: {sorted(duplicates)}")
        if not self.inclusions:
            raise ValueError("a protocol needs at least one inclusion criterion")
        if self.ranking_score and self.score_spec(self.ranking_score) is None:
            raise ValueError(f"ranking_score {self.ranking_score!r} is not one of the scores")
        return self

    @property
    def inclusions(self) -> list[Criterion]:
        return [c for c in self.criteria if c.kind == "inclusion"]

    @property
    def exclusions(self) -> list[Criterion]:
        return [c for c in self.criteria if c.kind == "exclusion"]

    @property
    def guards(self) -> list[Criterion]:
        return [c for c in self.criteria if c.kind == "guard"]

    def score_spec(self, question_id: str) -> ScoreSpec | None:
        return next((s for s in self.scores if s.id == question_id), None)

    def ranking_spec(self, default_id: str = "topical_fit") -> ScoreSpec | None:
        """The Score that ranks the reviewer's queue, if any. See `ranking_score`."""
        if self.ranking_score:
            return self.score_spec(self.ranking_score)
        return self.score_spec(default_id) or (self.scores[0] if self.scores else None)

    def criterion(self, question_id: str) -> Criterion | None:
        return next((c for c in self.criteria if c.id == question_id), None)

    @classmethod
    def load(cls, path: str | Path) -> Protocol:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls.model_validate(raw)

    def save(self, path: str | Path) -> None:
        """Write back as YAML. Used by the UI when criteria are edited."""
        payload = self.model_dump(mode="json", by_alias=True, exclude_none=True)
        Path(path).write_text(
            yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, width=100),
            encoding="utf-8",
        )
