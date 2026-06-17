"""Provider-neutral LLM-as-judge interface.

Judged metrics (Faithfulness, Answer Equivalence, Safety, Bias, Topic, Role,
G-Eval, ...) call this interface and never import a specific judge backend.
Concrete impls live in ``agent_evals.judges`` (Azure OpenAI direct, MLflow-native,
DeepEval), and are selectable per-metric via config so they can be A/B'd.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field


class JudgeVerdict(BaseModel):
    score: float                 # normalized to 0..1
    passed: bool | None = None
    rationale: str = ""
    raw: dict = Field(default_factory=dict)


@runtime_checkable
class Judge(Protocol):
    """Score a ``response`` against natural-language ``criteria``.

    Implementations must normalize their output to a 0..1 ``score`` and supply a
    short ``rationale``. ``question`` (the user prompt), ``context`` (grounding,
    e.g. tool outputs), and ``reference`` (a golden answer) are optional and
    passed when the metric provides them.
    """

    name: str

    def evaluate(
        self,
        *,
        criteria: str,
        response: str,
        question: str | None = None,
        context: str | None = None,
        reference: str | None = None,
    ) -> JudgeVerdict:
        ...
