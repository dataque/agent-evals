"""#5 Faithfulness — is every claim in the response supported by the tool
outputs (no fabrication)? Skipped when there are no tool outputs to ground on."""

from __future__ import annotations

from ..core.scorer import Family, Score, ScorerSpec, ScoringContext, TurnScope
from ._judge_base import judged, require_text, resolve_judge, tool_context

_CRITERIA = (
    "Are the factual claims in the RESPONSE about DATA (skills, roles, people, "
    "numbers) supported by the CONTEXT (the tool outputs the agent retrieved)? "
    "Penalize fabricated, unsupported, or contradicted data claims. Do NOT "
    "penalize: brief references to content shown in the UI cards/panels that the "
    "tool outputs back; standard product affordances the assistant legitimately "
    "offers (a side panel to review skills, a 'Download to send' email button, "
    "MyCareer, the jobs portal); or addressing the user by their own name. "
    "Score 1.0 = fully grounded, 0.0 = largely fabricated."
)


class Faithfulness:
    spec = ScorerSpec(
        metric="faithfulness", number=5, title="Faithfulness",
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
        if ctx.expectations.must_refuse:
            # a correct refusal makes no data claims to ground — refusal_correctness (#9) owns it
            return Score.skip(self.spec.metric, "must_refuse case — no data claims to ground")
        if (skip := require_text(ctx, self.spec.metric)):
            return skip
        context = tool_context(ctx.run)
        if not context:
            return Score.skip(self.spec.metric, "no tool outputs to ground against")
        return judged(self.spec.metric, judge, criteria=_CRITERIA,
                      response=ctx.run.assistant_text, question=ctx.question, context=context)
