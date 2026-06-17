"""#19 Plan Quality — the routing + tool set stays within the expected envelope.

Deterministic envelope check (ports the prior harness's ``plan_quality``):
average of (observed routes ⊆ allowed routes) and (observed tools ⊆ allowed
tools). Routes are synthesized by the transport from STEP/Task events.
"""

from __future__ import annotations

from ..core.scorer import Family, Score, ScorerSpec, ScoringContext, TurnScope


class PlanQuality:
    spec = ScorerSpec(
        metric="plan_quality", number=19, title="Plan Quality",
        family=Family.DETERMINISTIC, turn_scope=TurnScope.SINGLE,
        needs_golden=True, requires_fields=["subagent_routes", "tool_calls"],
    )

    def score(self, ctx: ScoringContext) -> Score:
        expected_routes = ctx.expectations.expected_routes
        allowed_tools = ctx.expectations.allowed_tool_calls
        if expected_routes is None and allowed_tools is None:
            return Score.skip(self.spec.metric, "no expected_routes / allowed_tool_calls")

        parts: list[float] = []
        details: dict = {}
        if expected_routes is not None:
            allowed = {str(r) for r in expected_routes}
            observed = {r.subagent for r in ctx.run.subagent_routes if r.subagent}
            parts.append((len(observed & allowed) / len(observed)) if observed else (1.0 if not allowed else 0.0))
            details["routes"] = {"allowed": sorted(allowed), "observed": sorted(observed)}
        if allowed_tools is not None:
            allowed = {str(t) for t in allowed_tools}
            observed = set(ctx.run.tool_names())
            parts.append((len(observed & allowed) / len(observed)) if observed else (1.0 if not allowed else 0.0))
            details["tools"] = {"allowed": sorted(allowed), "observed": sorted(observed)}

        value = sum(parts) / len(parts) if parts else None
        return Score(metric=self.spec.metric, value=value,
                     rationale=f"routes/tools within expected envelope", details=details).with_threshold(1.0)
