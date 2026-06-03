"""Tests for trace-aware scorers in evals.scorers."""

from __future__ import annotations


def _trace(events: list[dict]) -> dict:
    return {"schema": "hr-agent/Trace@v1", "trace_id": "t1", "events": events}


def _tool_call(name: str, *, args: dict | None = None, tool_call_id: str = "tc1") -> dict:
    return {
        "span_id": tool_call_id,
        "parent_span_id": None,
        "sequence": 0,
        "type": "tool_call",
        "agent_id": "agent",
        "timestamp": "2026-01-01T00:00:00Z",
        "data": {"tool_call_id": tool_call_id, "attempt": 0, "tool_name": name, "args": args or {}},
    }


def _tool_result(name: str, *, status: str = "ok", tool_call_id: str = "tc1") -> dict:
    return {
        "span_id": tool_call_id,
        "parent_span_id": None,
        "sequence": 1,
        "type": "tool_result",
        "agent_id": "agent",
        "timestamp": "2026-01-01T00:00:01Z",
        "data": {"tool_call_id": tool_call_id, "tool_name": name, "status": status, "result": {}, "error": None},
    }


def _route(to: str) -> dict:
    return {
        "span_id": "r1",
        "parent_span_id": None,
        "sequence": 0,
        "type": "route",
        "agent_id": "orchestrator",
        "timestamp": "2026-01-01T00:00:00Z",
        "data": {"route_to": to, "reason": ""},
    }


class TestToolTraceF1:
    def test_perfect_match(self):
        from evals.scorers import tool_trace_f1
        trace = _trace([_tool_call("a"), _tool_call("b", tool_call_id="tc2")])
        assert tool_trace_f1({"expected_tool_calls": ["a", "b"]}, trace) == 1.0

    def test_partial(self):
        from evals.scorers import tool_trace_f1
        trace = _trace([_tool_call("a")])
        score = tool_trace_f1({"expected_tool_calls": ["a", "b"]}, trace)
        assert 0 < score < 1

    def test_skips_when_no_expectation(self):
        from evals.scorers import tool_trace_f1
        assert tool_trace_f1({}, _trace([])) is None


class TestToolArgumentCorrectness:
    def test_subset_match(self):
        from evals.scorers import tool_argument_correctness
        trace = _trace([_tool_call("search_jobs", args={"q": "python", "extra": 1})])
        score = tool_argument_correctness(
            {"expected_tool_args": {"search_jobs": {"q": "python"}}}, trace
        )
        assert score == 1.0

    def test_missing_key_fails(self):
        from evals.scorers import tool_argument_correctness
        trace = _trace([_tool_call("search_jobs", args={"q": "python"})])
        score = tool_argument_correctness(
            {"expected_tool_args": {"search_jobs": {"q": "python", "loc": "ZRH"}}}, trace
        )
        assert score == 0.0

    def test_skipped_with_no_expectation(self):
        from evals.scorers import tool_argument_correctness
        assert tool_argument_correctness({}, _trace([])) is None


class TestStepEfficiency:
    def test_under_budget_is_one(self):
        from evals.scorers import step_efficiency
        trace = _trace([_tool_call("a")])
        assert step_efficiency({"max_steps": 5}, trace) == 1.0

    def test_over_budget_scales_down(self):
        from evals.scorers import step_efficiency
        trace = _trace([_tool_call("a", tool_call_id=str(i)) for i in range(10)])
        score = step_efficiency({"max_steps": 5}, trace)
        assert 0 < score < 1


class TestPlanQuality:
    def test_routes_within_envelope(self):
        from evals.scorers import plan_quality
        trace = _trace([_route("profile")])
        score = plan_quality({"expected_routes": ["profile"]}, trace)
        assert score == 1.0

    def test_off_route_zero(self):
        from evals.scorers import plan_quality
        trace = _trace([_route("outreach")])
        score = plan_quality({"expected_routes": ["profile"]}, trace)
        assert score == 0.0


class TestAuditLog:
    def test_action_taken_with_ok(self):
        from evals.scorers import audit_log_action_taken
        trace = _trace([_tool_call("persist_skills"), _tool_result("persist_skills")])
        score = audit_log_action_taken({"expected_actions": ["persist_skills"]}, trace)
        assert score == 1.0

    def test_action_with_error_fails(self):
        from evals.scorers import audit_log_action_taken
        trace = _trace([_tool_call("persist_skills"), _tool_result("persist_skills", status="error")])
        score = audit_log_action_taken({"expected_actions": ["persist_skills"]}, trace)
        assert score == 0.0


class TestCardFormat:
    def test_artifact_present_with_schema(self):
        from evals.scorers import card_format_correctness
        score = card_format_correctness(
            {"expected_artifacts": {"matched_jobs": "hr-agent/JobCard@v1"}},
            {"matched_jobs": {"schema": "hr-agent/JobCard@v1", "items": []}},
        )
        assert score == 1.0

    def test_artifact_missing(self):
        from evals.scorers import card_format_correctness
        score = card_format_correctness(
            {"expected_artifacts": {"matched_jobs": "hr-agent/JobCard@v1"}},
            {},
        )
        assert score == 0.0
