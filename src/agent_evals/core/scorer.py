"""The framework-neutral scoring contract.

A ``Scorer`` reads ``(EvalCase, RunRecord[])`` via a ``ScoringContext`` and
returns a ``Score``. Scorers are pure: no ``import mlflow``, no network unless a
``Judge`` is injected. ``ScorerSpec`` declares each scorer's requirements so the
runner can invoke it correctly (per-turn vs. whole-conversation) and skip it
gracefully when the evidence/golden it needs is missing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from .case import EvalCase, Expectations, Turn
from .judge import Judge
from .run_record import RunRecord


class Family(str, Enum):
    DETERMINISTIC = "deterministic"
    JUDGE = "judge"
    OPERATIONAL = "operational"
    PROBE = "probe"


class TurnScope(str, Enum):
    SINGLE = "single"  # scored once per turn
    MULTI = "multi"    # scored once over the whole conversation (at the last turn)
    BOTH = "both"      # treated as SINGLE by the runner


class Score(BaseModel):
    """The result of one scorer invocation. ``value`` is normalized to 0..1
    (None when skipped or errored). ``passed``/``threshold`` are optional."""

    metric: str
    value: float | None = None
    passed: bool | None = None
    threshold: float | None = None
    rationale: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    skipped: bool = False
    skip_reason: str | None = None

    @classmethod
    def skip(cls, metric: str, reason: str, **details: Any) -> "Score":
        return cls(metric=metric, skipped=True, skip_reason=reason, details=details)

    @classmethod
    def failed(cls, metric: str, error: str, **details: Any) -> "Score":
        return cls(metric=metric, error=error, details=details)

    def with_threshold(self, threshold: float | None) -> "Score":
        """Set ``passed`` from ``value >= threshold`` when both are present."""
        self.threshold = threshold
        if threshold is not None and self.value is not None:
            self.passed = self.value >= threshold
        return self


class ScorerSpec(BaseModel):
    metric: str                       # canonical id, e.g. "tool_selection_accuracy"
    number: int                       # the # from docs/metrics.md
    title: str                        # human label
    family: Family
    turn_scope: TurnScope = TurnScope.SINGLE
    needs_golden: bool = False
    needs_judge: bool = False
    requires_fields: list[str] = Field(default_factory=list)


@dataclass
class ScoringContext:
    """What a scorer sees. For single-turn scorers the runner sets
    ``turn_index`` to each turn in succession; for multi-turn scorers it is the
    final turn, with the full conversation available in ``runs``."""

    case: EvalCase
    runs: list[RunRecord]
    turn_index: int = 0
    judge: Judge | None = None
    config: dict = field(default_factory=dict)

    @property
    def run(self) -> RunRecord:
        return self.runs[self.turn_index]

    @property
    def turn(self) -> Turn:
        return self.case.as_turns()[self.turn_index]

    @property
    def expectations(self) -> Expectations:
        return self.turn.expectations

    @property
    def question(self) -> str:
        return self.turn.question

    @property
    def is_last_turn(self) -> bool:
        return self.turn_index >= len(self.runs) - 1


@runtime_checkable
class Scorer(Protocol):
    spec: ScorerSpec

    def score(self, ctx: ScoringContext) -> Score:
        ...


class CaseResult(BaseModel):
    case_id: str
    scores: list[Score] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
