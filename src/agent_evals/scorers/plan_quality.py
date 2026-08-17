"""#19 Plan Quality — the routing + tool set stays within the expected envelope.

Deterministic envelope check (ports the prior harness's ``plan_quality``):
average of (observed routes ⊆ allowed routes) and (observed tools ⊆ allowed
tools). Routes are synthesized by the transport from STEP/Task events.
"""

from __future__ import annotations

from ..core.scorer import (
    Family,
    Score,
    ScorerSpec,
    ScoringContext,
    TurnScope,
    filter_infrastructure,
)


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

        # Containment, not coverage: this metric asks whether the agent stayed
        # INSIDE the envelope, never whether it used everything in it. An empty
        # observed set is therefore trivially contained (∅ ⊆ allowed) and scores
        # 1.0, including when `allowed` is non-empty. Scoring 0.0 there punished
        # a turn for answering from context without a tool call, which is exactly
        # what a well-behaved agent does when it already has what it needs (E7).
        parts: list[float] = []
        details: dict = {}
        if expected_routes is not None:
            allowed = {str(r) for r in expected_routes}
            observed = {r.subagent for r in ctx.run.subagent_routes if r.subagent}
            outside = sorted(observed - allowed)
            parts.append((len(observed & allowed) / len(observed)) if observed else 1.0)
            details["routes"] = {"allowed": sorted(allowed), "observed": sorted(observed),
                                 "outside_envelope": outside}
            # A route breach is a different KIND of event from an extra tool call:
            # it means the request reached an agent the case never sanctioned,
            # potentially another persona's. Flag it so the runner can report it
            # on its own rather than averaging it into this metric's value, where
            # an offsetting improvement elsewhere can cancel it out entirely (E18).
            if outside:
                details["route_violation"] = True
        if allowed_tools is not None:
            allowed = {str(t) for t in allowed_tools}
            observed = filter_infrastructure(ctx.run.tool_names(), ctx.config)
            outside = sorted(observed - allowed)
            parts.append((len(observed & allowed) / len(observed)) if observed else 1.0)
            details["tools"] = {"allowed": sorted(allowed), "observed": sorted(observed),
                                "outside_envelope": outside}

        value = sum(parts) / len(parts) if parts else None
        # Name which branch failed, so a route breach and a stray tool are never
        # read as the same thing at equal value.
        broke = [k for k in ("routes", "tools") if details.get(k, {}).get("outside_envelope")]
        rationale = (f"outside expected envelope: {', '.join(broke)}" if broke
                     else "routes/tools within expected envelope")
        return Score(metric=self.spec.metric, value=value,
                     rationale=rationale, details=details).with_threshold(1.0)
