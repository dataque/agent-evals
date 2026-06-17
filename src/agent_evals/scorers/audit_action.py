"""#16 Audit Log / Action Taken — every expected mutating tool fired and
returned OK. Note: confirms the action ran with an ok result; it does not (yet)
verify the persisted DB side effect (see plan: future side-effect verifier)."""

from __future__ import annotations

from ..core.run_record import ToolStatus
from ..core.scorer import Family, Score, ScorerSpec, ScoringContext, TurnScope


class AuditLogActionTaken:
    spec = ScorerSpec(
        metric="audit_log_action_taken",
        number=16,
        title="Audit Log / Action Taken Correctness",
        family=Family.DETERMINISTIC,
        turn_scope=TurnScope.SINGLE,
        needs_golden=True,
        requires_fields=["tool_calls"],
    )

    def score(self, ctx: ScoringContext) -> Score:
        expected = ctx.expectations.expected_actions
        if not expected:
            return Score.skip(self.spec.metric, "no expected_actions in expectations")
        ok_tools = {
            tc.name
            for tc in ctx.run.tool_calls
            if tc.status == ToolStatus.OK and not tc.is_error and tc.name
        }
        matched = [a for a in expected if a in ok_tools]
        missed = [a for a in expected if a not in ok_tools]
        value = len(matched) / len(expected)
        return Score(
            metric=self.spec.metric,
            value=value,
            rationale=f"ran_ok={matched} missing_or_failed={missed}",
            details={"matched": matched, "missed": missed, "ok_tools": sorted(t for t in ok_tools)},
        ).with_threshold(1.0)
