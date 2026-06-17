"""#11 Knowledge Retention — multi-turn. Does the assistant remember and reuse
information shared in earlier turns?"""

from __future__ import annotations

from ..core.scorer import Family, Score, ScorerSpec, ScoringContext, TurnScope
from ._judge_base import judged, resolve_judge, transcript

_CRITERIA = (
    "Below is a full multi-turn CONVERSATION. Did the assistant correctly remember "
    "and use information the user shared in EARLIER turns when responding to LATER "
    "turns (no forgetting, no asking again for already-provided facts)? "
    "If a REFERENCE list of facts is given, verify each was retained. Score 1.0 = "
    "full retention."
)


class KnowledgeRetention:
    spec = ScorerSpec(
        metric="knowledge_retention", number=11, title="Knowledge Retention",
        family=Family.JUDGE, turn_scope=TurnScope.MULTI,
        needs_judge=True, requires_fields=["messages"],
    )

    def __init__(self, judge=None):
        self.judge = judge

    def score(self, ctx: ScoringContext) -> Score:
        judge = resolve_judge(self, ctx)
        if judge is None:
            return Score.skip(self.spec.metric, "no judge configured")
        if len(ctx.runs) < 2:
            return Score.skip(self.spec.metric, "needs at least 2 turns")
        convo = transcript(ctx.runs)
        facts = ctx.expectations.remembered_facts
        reference = "\n".join(f"- {f}" for f in facts) if facts else None
        return judged(self.spec.metric, judge, criteria=_CRITERIA, response=convo,
                      reference=reference, extra={"turns": len(ctx.runs)})
