"""#3 Tool Argument Correctness — fraction of expected tools whose observed
args satisfy the expected key/values (subset match; extra args tolerated)."""

from __future__ import annotations

from ..core.scorer import Family, Score, ScorerSpec, ScoringContext, TurnScope


class ToolArgumentCorrectness:
    spec = ScorerSpec(
        metric="tool_argument_correctness",
        number=3,
        title="Tool Argument Correctness",
        family=Family.DETERMINISTIC,
        turn_scope=TurnScope.SINGLE,
        needs_golden=True,
        requires_fields=["tool_calls"],
    )

    def score(self, ctx: ScoringContext) -> Score:
        expected = ctx.expectations.expected_tool_args
        if not expected:
            return Score.skip(self.spec.metric, "no expected_tool_args in expectations")

        calls_by_name: dict[str, list[dict]] = {}
        for tc in ctx.run.tool_calls:
            calls_by_name.setdefault(tc.name or "", []).append(tc.args or {})

        matched: list[str] = []
        missed: list[str] = []
        for tool_name, want_args in expected.items():
            candidates = calls_by_name.get(tool_name, [])
            ok = any(
                all(obs.get(k) == v for k, v in (want_args or {}).items())
                for obs in candidates
            )
            (matched if ok else missed).append(tool_name)

        value = len(matched) / len(expected)
        return Score(
            metric=self.spec.metric,
            value=value,
            rationale=f"matched={matched} missed={missed}",
            details={"matched": matched, "missed": missed},
        ).with_threshold(1.0)
