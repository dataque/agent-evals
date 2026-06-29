"""#10 Conversation Completeness — multi-turn. Did every distinct user intent
across the conversation get addressed (recall)?"""

from __future__ import annotations

from ..core.scorer import Family, Score, ScorerSpec, ScoringContext, TurnScope
from ._judge_base import judged, resolve_judge, transcript

_CRITERIA = (
    "Below is a full multi-turn CONVERSATION between a user and an assistant. "
    "Did the assistant address every distinct user intent/request across the "
    "whole conversation? A correct refusal or redirect of an out-of-scope, "
    "unsafe, or unsupported request COUNTS as appropriately addressing that "
    "intent. Score 1.0 if every user request received an appropriate response, "
    "lower only if an in-scope intent was dropped or ignored."
)


class ConversationCompleteness:
    spec = ScorerSpec(
        metric="conversation_completeness", number=10, title="Conversation Completeness",
        family=Family.JUDGE, turn_scope=TurnScope.MULTI,
        needs_judge=True, requires_fields=["messages"],
    )

    def __init__(self, judge=None):
        self.judge = judge

    def score(self, ctx: ScoringContext) -> Score:
        judge = resolve_judge(self, ctx)
        if judge is None:
            return Score.skip(self.spec.metric, "no judge configured")
        if len(ctx.runs) < 1:
            return Score.skip(self.spec.metric, "no turns")
        convo = transcript(ctx.runs)
        return judged(self.spec.metric, judge, criteria=_CRITERIA, response=convo,
                      extra={"turns": len(ctx.runs)})
