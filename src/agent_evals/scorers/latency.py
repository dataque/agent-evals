"""#13 Latency — operational. Per-run TTFT / total / abort are surfaced in
details; P50/P95/P99 across runs are computed by the runner's aggregation.

If ``latency_total_sla_ms`` is set in config, the per-run value becomes a
pass/fail against that SLA; otherwise value is None (operational, non-quality).
"""

from __future__ import annotations

from ..core.scorer import Family, Score, ScorerSpec, ScoringContext, TurnScope


class Latency:
    spec = ScorerSpec(
        metric="latency",
        number=13,
        title="Latency (TTFT + stream completion)",
        family=Family.OPERATIONAL,
        turn_scope=TurnScope.SINGLE,
        requires_fields=["timing"],
    )

    def score(self, ctx: ScoringContext) -> Score:
        t = ctx.run.timing
        details = {
            "ttft_ms": t.ttft_ms,
            "total_ms": t.total_ms,
            "request_to_run_started_ms": t.request_to_run_started_ms,
            "aborted": t.aborted,
            "completion_status": ctx.run.completion_status.value,
        }
        sla = ctx.config.get("latency_total_sla_ms")
        if sla and t.total_ms is not None:
            value = 1.0 if t.total_ms <= float(sla) else 0.0
            return Score(
                metric=self.spec.metric, value=value, details=details,
                rationale=f"total={t.total_ms:.0f}ms sla={sla}ms",
            ).with_threshold(1.0)
        return Score(
            metric=self.spec.metric, value=None, details=details,
            rationale=f"ttft={t.ttft_ms} total={t.total_ms} (operational)",
        )
