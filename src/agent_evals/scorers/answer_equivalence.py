"""#6 Answer Equivalence — LLM-judged semantic match to a reference answer."""

from __future__ import annotations

from ..core.scorer import Family, Score, ScorerSpec, ScoringContext, TurnScope
from ._judge_base import judged, require_text, resolve_judge

_CRITERIA = (
    "Does the RESPONSE convey the same correct answer as the REFERENCE? "
    "Judge semantic equivalence and factual correctness, not wording. "
    "Score 1.0 if equivalent and correct, partial credit for partially correct."
)


class AnswerEquivalence:
    spec = ScorerSpec(
        metric="answer_equivalence", number=6, title="Answer Equivalence",
        family=Family.JUDGE, turn_scope=TurnScope.SINGLE,
        needs_golden=True, needs_judge=True, requires_fields=["assistant_text"],
    )

    def __init__(self, judge=None):
        self.judge = judge

    def score(self, ctx: ScoringContext) -> Score:
        ref = ctx.expectations.expected_response
        if not ref:
            return Score.skip(self.spec.metric, "no expected_response (golden) in expectations")
        judge = resolve_judge(self, ctx)
        if judge is None:
            return Score.skip(self.spec.metric, "no judge configured")
        if (skip := require_text(ctx, self.spec.metric)):
            return skip
        return judged(self.spec.metric, judge, criteria=_CRITERIA,
                      response=ctx.run.assistant_text, question=ctx.question, reference=ref)
