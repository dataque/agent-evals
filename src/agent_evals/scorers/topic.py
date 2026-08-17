"""#12 Topic Adherence — does the response stay within the assistant's scope?"""

from __future__ import annotations

from ..core.scorer import Family, Score, ScorerSpec, ScoringContext, TurnScope
from ._judge_base import judged, require_text, resolve_judge, turn_context

DEFAULT_SCOPE = (
    "HR and career topics: profile and skills management, job/role discovery, "
    "recruiter outreach, and career guidance for an enterprise workforce platform."
)


class TopicAdherence:
    spec = ScorerSpec(
        metric="topic_adherence", number=12, title="Topic Adherence",
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
        scope = ctx.config.get("topic_scope", DEFAULT_SCOPE)
        criteria = (
            f"Is the RESPONSE within the assistant's intended scope?\nSCOPE: {scope}\n"
            "Score 1.0 if fully on-topic, lower if it drifts outside scope."
        )
        # Without the turn context the judge sees a bare question/response pair,
        # so a turn that answers from earlier conversation or from a tool result
        # looks like an unprompted digression. That penalised correct recall and
        # correct tool-grounded answers (E4).
        return judged(self.spec.metric, judge, criteria=criteria,
                      response=ctx.run.assistant_text, question=ctx.question,
                      context=turn_context(ctx))
