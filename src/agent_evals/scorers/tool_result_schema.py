"""#4 Tool Result Schema Adherence — every emitted tool result validates against
the schema the frontend declares for that tool (the contracts registry)."""

from __future__ import annotations

from ..contracts.registry import ContractRegistry, get_registry
from ..core.scorer import Family, Score, ScorerSpec, ScoringContext, TurnScope


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

    def __init__(self, registry: ContractRegistry | None = None) -> None:
        self._registry = registry

    @property
    def registry(self) -> ContractRegistry:
        return self._registry or get_registry()

    def score(self, ctx: ScoringContext) -> Score:
        reg = self.registry
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
        if checked == 0:
            return Score.skip(self.spec.metric, "no tool results with a registered schema")
        value = valid / checked
        return Score(
            metric=self.spec.metric,
            value=value,
            rationale=f"{valid}/{checked} tool results valid",
            details={"checked": checked, "valid": valid, "failures": failures,
                     "registered_tools": reg.tools()},
        ).with_threshold(1.0)
