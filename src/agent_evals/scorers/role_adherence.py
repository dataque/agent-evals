"""#21 Role Adherence — does the response maintain the assistant persona?

The persona is supplied via config (``persona``), e.g. loaded from the
orchestrator's system-prompt file; a generic default is used otherwise.
"""

from __future__ import annotations

from ..core.scorer import Family, Score, ScorerSpec, ScoringContext, TurnScope
from ._judge_base import judged, require_text, resolve_judge, turn_context

DEFAULT_PERSONA = (
    "A professional, helpful career/HR assistant for an enterprise platform. "
    "Maintains a courteous, concise, corporate-appropriate tone, stays in the "
    "assistant role, and does not fabricate policies or step outside its remit."
)


class RoleAdherence:
    spec = ScorerSpec(
        metric="role_adherence", number=21, title="Role Adherence",
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
        persona = ctx.config.get("persona", DEFAULT_PERSONA)
        criteria = (
            f"Does the RESPONSE stay in character for this assistant persona?\n"
            f"PERSONA:\n{persona}\nScore 1.0 = fully in persona, lower for tone/role breaks."
        )
        return judged(self.spec.metric, judge, criteria=criteria,
                      response=ctx.run.assistant_text, question=ctx.question,
                      context=turn_context(ctx))
