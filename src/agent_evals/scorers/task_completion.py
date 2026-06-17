"""#1 Task Completion — did the agent accomplish the user's goal this turn,
considering the response and any tool actions taken? Golden is optional."""

from __future__ import annotations

from ..core.scorer import Family, Score, ScorerSpec, ScoringContext, TurnScope
from ._judge_base import judged, resolve_judge, tool_context

_CRITERIA = (
    "Did the assistant fully accomplish the user's goal for this turn? "
    "Consider the user's QUESTION, the assistant's RESPONSE, and the tool actions "
    "in CONTEXT. If a REFERENCE is given, treat it as the ideal outcome. "
    "Score 1.0 for full completion, partial credit otherwise."
)


class TaskCompletion:
    spec = ScorerSpec(
        metric="task_completion", number=1, title="Task Completion",
        family=Family.JUDGE, turn_scope=TurnScope.SINGLE,
        needs_golden=False, needs_judge=True,
        requires_fields=["assistant_text", "tool_calls"],
    )

    def __init__(self, judge=None):
        self.judge = judge

    def score(self, ctx: ScoringContext) -> Score:
        judge = resolve_judge(self, ctx)
        if judge is None:
            return Score.skip(self.spec.metric, "no judge configured")
        run = ctx.run
        if not (run.assistant_text or "").strip() and not run.tool_calls:
            return Score.skip(self.spec.metric, "no response or tool actions to judge")
        return judged(
            self.spec.metric, judge, criteria=_CRITERIA,
            response=run.assistant_text or "(no text; tool-only turn)",
            question=ctx.question,
            context=tool_context(run) or None,
            reference=ctx.expectations.expected_response,
        )
