"""#18 Step / Tool-Call Efficiency — did the agent reach the goal within the
expected action budget? "Steps" here are tool calls (the agent's actions)."""

from __future__ import annotations

from ..core.scorer import Family, Score, ScorerSpec, ScoringContext, TurnScope


class StepEfficiency:
    spec = ScorerSpec(
        metric="step_efficiency",
        number=18,
        title="Step / Tool-Call Efficiency",
        family=Family.DETERMINISTIC,
        turn_scope=TurnScope.SINGLE,
        needs_golden=True,
        requires_fields=["tool_calls"],
    )

    def score(self, ctx: ScoringContext) -> Score:
        max_steps = ctx.expectations.max_steps
        if max_steps is None:
            return Score.skip(self.spec.metric, "no max_steps in expectations")
        observed = len(ctx.run.tool_calls)
        if observed == 0:
            value = 1.0
        elif observed <= int(max_steps):
            value = 1.0
        else:
            value = max(0.0, int(max_steps) / observed)
        return Score(
            metric=self.spec.metric,
            value=value,
            rationale=f"observed={observed} budget={max_steps}",
            details={"observed_steps": observed, "max_steps": int(max_steps)},
        ).with_threshold(1.0)
