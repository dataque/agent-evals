"""The eval dataset schema: ``EvalCase`` (single- or multi-turn) and the
per-metric expectation/golden fields.

Field names intentionally mirror the prior MLflow harness so existing datasets
port over, with a few additions for metrics that harness did not cover
(isolation probe, refusal, knowledge retention, free-form rubric).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Expectations(BaseModel):
    """Optional golden/expectation values for a turn. Every field is optional;
    a scorer skips gracefully when the field it needs is absent.

    ``extra='allow'`` keeps forward-compat with dataset keys we have not modeled.
    """

    model_config = ConfigDict(extra="allow")

    # Golden / reference text
    expected_response: str | None = None         # #1, #6 (semantic match)
    response_must_contain: list[str] | None = None  # #22 string check
    forbidden_substrings: list[str] | None = None   # #7/#22 negative check

    # Tool expectations
    expected_tool_calls: list[str] | None = None        # #2 tool selection
    expected_tool_args: dict[str, dict[str, Any]] | None = None  # #3 tool args
    allowed_tool_calls: list[str] | None = None         # #19 plan quality envelope
    expected_actions: list[str] | None = None           # #16 audit log / action taken
    expected_artifacts: dict[str, str] | None = None     # {name: schema_id}

    # Planning / efficiency
    max_steps: int | None = None                 # #18 step efficiency
    expected_routes: list[str] | None = None     # #19 plan quality routing

    # Multi-turn / behavioral
    remembered_facts: list[str] | None = None    # #11 knowledge retention
    must_refuse: bool | None = None              # #9 refusal correctness
    expected_redirect: str | None = None         # #9 expected redirect topic
    other_user_id: str | None = None             # #8 cross-user isolation probe

    # Free-form judge rubric (#17 G-Eval)
    rubric: str | None = None


class Turn(BaseModel):
    question: str
    expectations: Expectations = Field(default_factory=Expectations)


class EvalCase(BaseModel):
    """One eval scenario. Single-turn sets ``question``; multi-turn sets
    ``turns``. Use :meth:`as_turns` to iterate uniformly."""

    id: str
    question: str | None = None
    expectations: Expectations = Field(default_factory=Expectations)
    scenario: str | None = None
    turns: list[Turn] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_multi_turn(self) -> bool:
        return bool(self.turns)

    def as_turns(self) -> list[Turn]:
        if self.turns:
            return self.turns
        return [Turn(question=self.question or "", expectations=self.expectations)]

    @classmethod
    def from_raw(cls, raw: dict, *, id: str) -> "EvalCase":
        """Parse a chat-evals-style dict:

        single-turn: ``{"inputs": {"question": ...}, "expectations": {...}}``
        multi-turn:  ``{"inputs": {"scenario": ..., "turns": [{question, expectations}]}}``
        """
        inputs = raw.get("inputs", {}) or {}
        metadata = dict(raw.get("metadata", {}) or {})
        if "turns" in inputs:
            turns = [
                Turn(
                    question=t.get("question", ""),
                    expectations=Expectations(**(t.get("expectations", {}) or {})),
                )
                for t in (inputs.get("turns") or [])
            ]
            return cls(
                id=id,
                scenario=inputs.get("scenario"),
                turns=turns,
                metadata=metadata,
            )
        return cls(
            id=id,
            question=inputs.get("question", ""),
            expectations=Expectations(**(raw.get("expectations", {}) or {})),
            metadata=metadata,
        )
