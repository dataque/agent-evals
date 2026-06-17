"""#14 Token / Cost — operational. Surfaces per-run usage (flagged reported vs.
estimated); the runner aggregates token sums/means across the run."""

from __future__ import annotations

from ..core.scorer import Family, Score, ScorerSpec, ScoringContext, TurnScope


class TokenCost:
    spec = ScorerSpec(
        metric="token_cost", number=14, title="Token / Cost",
        family=Family.OPERATIONAL, turn_scope=TurnScope.SINGLE,
        requires_fields=["usage"],
    )

    def score(self, ctx: ScoringContext) -> Score:
        u = ctx.run.usage
        return Score(
            metric=self.spec.metric,
            value=None,  # operational, not a 0..1 quality score
            rationale=f"{u.total_tokens} tokens ({u.source.value})"
            + (f", ${u.cost_usd:.4f}" if u.cost_usd is not None else ""),
            details={
                "source": u.source.value,
                "input_tokens": u.input_tokens,
                "output_tokens": u.output_tokens,
                "total_tokens": u.total_tokens,
                "cost_usd": u.cost_usd,
                "estimator": u.estimator,
                "by_subagent": u.by_subagent,
            },
        )
