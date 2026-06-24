"""#2 Tool Selection Accuracy — F1 of observed vs. expected tool names."""

from __future__ import annotations

from ..core.aggregate import precision_recall_f1
from ..core.scorer import (
    Family,
    Score,
    ScorerSpec,
    ScoringContext,
    TurnScope,
    filter_infrastructure,
)


class ToolSelectionAccuracy:
    spec = ScorerSpec(
        metric="tool_selection_accuracy",
        number=2,
        title="Tool Selection Accuracy",
        family=Family.DETERMINISTIC,
        turn_scope=TurnScope.SINGLE,
        needs_golden=True,
        requires_fields=["tool_calls"],
    )

    def score(self, ctx: ScoringContext) -> Score:
        expected = ctx.expectations.expected_tool_calls
        if expected is None:
            return Score.skip(self.spec.metric, "no expected_tool_calls in expectations")
        observed = filter_infrastructure(ctx.run.tool_names(), ctx.config)
        exp = filter_infrastructure(expected, ctx.config)
        precision, recall, fv = precision_recall_f1(exp, observed)
        return Score(
            metric=self.spec.metric,
            value=fv,
            rationale=f"expected={sorted(exp)} observed={sorted(observed)}",
            details={
                "precision": precision,
                "recall": recall,
                "expected": sorted(exp),
                "observed": sorted(observed),
                "missing": sorted(exp - observed),
                "unexpected": sorted(observed - exp),
            },
        ).with_threshold(1.0)
