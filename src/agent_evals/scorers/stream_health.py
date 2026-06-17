"""#24 Stream Health Detail — protocol-invariant integrity of the event stream
(lifecycle bracketing, tool/text bracketing, arg integrity, state-patch
integrity). Score = fraction of invariant checks satisfied."""

from __future__ import annotations

from ..core.scorer import Family, Score, ScorerSpec, ScoringContext, TurnScope


class StreamHealthDetail:
    spec = ScorerSpec(
        metric="stream_health",
        number=24,
        title="Stream Health Detail",
        family=Family.DETERMINISTIC,
        turn_scope=TurnScope.SINGLE,
        requires_fields=["stream_health", "events"],
    )

    def score(self, ctx: ScoringContext) -> Score:
        h = ctx.run.stream_health
        checks = {
            "run_started": h.run_started_seen,
            "run_finished": h.run_finished_seen,
            "finished_not_truncated": not h.ended_before_finished,
            "no_duplicate_run_started": not h.duplicate_run_started,
            "no_ordering_violations": not h.ordering_violations,
            "tool_calls_bracketed": not h.unmatched_tool_starts and not h.unmatched_tool_ends,
            "tool_results_present": not h.tool_calls_missing_result,
            "no_orphan_results": not h.orphan_tool_results,
            "args_well_formed": not h.malformed_arg_tool_calls,
            "state_patches_applied": not h.state_patch_errors,
        }
        passed = sum(1 for ok in checks.values() if ok)
        value = passed / len(checks)
        failed = [name for name, ok in checks.items() if not ok]
        return Score(
            metric=self.spec.metric,
            value=value,
            rationale="clean stream" if not failed else f"violations: {failed}",
            details={
                "checks": checks,
                "failed": failed,
                "ordering_violations": h.ordering_violations,
                "unmatched_tool_starts": h.unmatched_tool_starts,
                "tool_calls_missing_result": h.tool_calls_missing_result,
                "malformed_arg_tool_calls": h.malformed_arg_tool_calls,
                "state_patch_errors": h.state_patch_errors,
                "unknown_event_types": h.unknown_event_types,
            },
        ).with_threshold(1.0)
