"""#20 Answer Relevancy — reference-free relevance of the response to the query."""

from __future__ import annotations

from ..core.scorer import Family, Score, ScorerSpec, ScoringContext, TurnScope
from ._judge_base import judged, require_text, resolve_judge, turn_context

_CRITERIA = (
    "Is the RESPONSE relevant and directly responsive to the QUESTION? "
    "Ignore correctness; judge only on-topic relevance and responsiveness. "
    "Score 1.0 = fully relevant, 0.0 = irrelevant."
)


class AnswerRelevancy:
    spec = ScorerSpec(
        metric="answer_relevancy", number=20, title="Answer Relevancy",
        family=Family.JUDGE, turn_scope=TurnScope.SINGLE,
        needs_judge=True, requires_fields=["assistant_text"],
    )

    def __init__(self, judge=None):
        self.judge = judge

    def score(self, ctx: ScoringContext) -> Score:
        judge = resolve_judge(self, ctx)
        if judge is None:
            return Score.skip(self.spec.metric, "no judge configured")
        if (skip := require_text(ctx, self.spec.metric)):
            return skip
        return judged(self.spec.metric, judge, criteria=_CRITERIA,
                      response=ctx.run.assistant_text, question=ctx.question,
                      context=turn_context(ctx))
