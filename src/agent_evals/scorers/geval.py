"""#17 G-Eval / Rubric Scoring — judge against a custom rubric supplied per-case
(``expectations.rubric``) or run-wide (``config['rubric']``)."""

from __future__ import annotations

from ..core.scorer import Family, Score, ScorerSpec, ScoringContext, TurnScope
from ._judge_base import judged, require_text, resolve_judge, tool_context


class GEval:
    spec = ScorerSpec(
        metric="geval", number=17, title="G-Eval / Rubric Scoring",
        family=Family.JUDGE, turn_scope=TurnScope.SINGLE,
        needs_judge=True, requires_fields=["assistant_text"],
    )

    def __init__(self, judge=None):
        self.judge = judge

    def score(self, ctx: ScoringContext) -> Score:
        rubric = ctx.expectations.rubric or ctx.config.get("rubric")
        if not rubric:
            return Score.skip(self.spec.metric, "no rubric in expectations or config")
        judge = resolve_judge(self, ctx)
        if judge is None:
            return Score.skip(self.spec.metric, "no judge configured")
        if (skip := require_text(ctx, self.spec.metric)):
            return skip
        return judged(self.spec.metric, judge, criteria=rubric,
                      response=ctx.run.assistant_text, question=ctx.question,
                      context=tool_context(ctx.run) or None,
                      reference=ctx.expectations.expected_response)
