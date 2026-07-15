"""#4 Tool Result Schema Adherence — every emitted tool result validates against
the schema the frontend declares for that tool (the contracts registry), and
every state-bearing tool leaves its session-state property valid (the state
registry).

The skills tools return only a ``{status, data:{result}}`` acknowledgement — the
payload the frontend renders is written to session state (surfaced via AG-UI
STATE_SNAPSHOT), so their contract is checked in two parts: the ack shape on the
tool result, and the ``skills`` property on ``RunRecord.final_state``.
"""

from __future__ import annotations

from ..contracts.registry import ContractRegistry, get_registry, get_state_registry
from ..core.scorer import Family, Score, ScorerSpec, ScoringContext, TurnScope

# Tools whose user-facing payload lives in session state, not the tool result:
# tool name -> the state property it must leave valid. ``save_skills`` READS the
# property rather than writing it, so it is deliberately not listed.
STATE_PROP_BY_TOOL: dict[str, str] = {
    "get_skills": "skills",
    "suggest_skills": "skills",
    "edit_skills": "skills",
}


class ToolResultSchemaAdherence:
    spec = ScorerSpec(
        metric="tool_result_schema_adherence",
        number=4,
        title="Tool Result Schema Adherence",
        family=Family.DETERMINISTIC,
        turn_scope=TurnScope.SINGLE,
        needs_golden=False,
        requires_fields=["tool_calls"],
    )

    def __init__(
        self,
        registry: ContractRegistry | None = None,
        state_registry: ContractRegistry | None = None,
    ) -> None:
        self._registry = registry
        self._state_registry = state_registry

    @property
    def registry(self) -> ContractRegistry:
        return self._registry or get_registry()

    @property
    def state_registry(self) -> ContractRegistry:
        return self._state_registry or get_state_registry()

    def score(self, ctx: ScoringContext) -> Score:
        reg = self.registry
        sreg = self.state_registry
        checked = 0
        valid = 0
        failures: list[dict] = []

        for tc in ctx.run.tool_calls:
            if not reg.has(tc.name):
                continue
            checked += 1
            ok, err = reg.validate(tc.name, tc.result)
            if ok:
                valid += 1
            else:
                failures.append({"tool": tc.name, "tool_call_id": tc.tool_call_id, "error": err})

        # state contracts: one check per distinct state property written this turn
        state_props: dict[str, list[str]] = {}
        for tc in ctx.run.tool_calls:
            prop = STATE_PROP_BY_TOOL.get((tc.name or "").lower())
            if prop and sreg.has(prop):
                state_props.setdefault(prop, []).append(tc.name or "")
        state = ctx.run.final_state if isinstance(ctx.run.final_state, dict) else {}
        for prop, tool_names in sorted(state_props.items()):
            checked += 1
            writers = "/".join(sorted(set(tool_names)))
            payload = state.get(prop)
            if payload is None:
                failures.append({
                    "state_property": prop,
                    "tools": sorted(set(tool_names)),
                    "error": f"final_state['{prop}'] absent after {writers} "
                             "(no STATE_SNAPSHOT captured, or the backend did not write it)",
                })
                continue
            ok, err = sreg.validate(prop, payload)
            if ok:
                valid += 1
            else:
                failures.append({"state_property": prop, "tools": sorted(set(tool_names)), "error": err})

        if checked == 0:
            return Score.skip(self.spec.metric, "no tool results with a registered schema")
        value = valid / checked
        return Score(
            metric=self.spec.metric,
            value=value,
            rationale=f"{valid}/{checked} contract checks valid",
            details={"checked": checked, "valid": valid, "failures": failures,
                     "state_properties_checked": sorted(state_props),
                     "registered_tools": reg.tools()},
        ).with_threshold(1.0)
