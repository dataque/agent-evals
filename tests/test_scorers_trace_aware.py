"""Tests for trace-aware scorers. Ported from chat-evals/evals/tests/test_scorers_trace.py."""

from __future__ import annotations

from agent_evals.scorers import (
    audit_log_action_taken,
    card_format_correctness,
    plan_quality,
    step_efficiency,
    tool_argument_correctness,
    tool_trace_f1,
)


def _tool_call(name: str, args: dict | None = None) -> dict:
    return {"type": "tool_call", "data": {"tool_name": name, "args": args or {}}}


def _tool_result(name: str, status: str = "ok") -> dict:
    return {"type": "tool_result", "data": {"tool_name": name, "status": status}}


def _route(route_to: str) -> dict:
    return {"type": "route", "data": {"route_to": route_to}}


def _trace(events: list[dict]) -> dict:
    return {"schema": "hr-agent/Trace@v1", "events": events}


# ---------------------------------------------------------------------------
# tool_trace_f1
# ---------------------------------------------------------------------------


class TestToolTraceF1:
    def test_returns_none_without_expectation(self):
        assert tool_trace_f1({}, _trace([])) is None

    def test_perfect_match(self):
        trace = _trace([_tool_call("a"), _tool_call("b")])
        assert tool_trace_f1({"expected_tool_calls": ["a", "b"]}, trace) == 1.0

    def test_missing_tool(self):
        trace = _trace([_tool_call("a")])
        score = tool_trace_f1({"expected_tool_calls": ["a", "b"]}, trace)
        assert 0.0 < score < 1.0


class TestToolArgumentCorrectness:
    def test_returns_none_without_expectation(self):
        assert tool_argument_correctness({}, _trace([])) is None

    def test_match(self):
        trace = _trace([_tool_call("save_skills", {"version": 3, "skills": ["python"]})])
        score = tool_argument_correctness(
            {"expected_tool_args": {"save_skills": {"version": 3}}}, trace
        )
        assert score == 1.0

    def test_wrong_arg_value(self):
        trace = _trace([_tool_call("save_skills", {"version": 99})])
        score = tool_argument_correctness(
            {"expected_tool_args": {"save_skills": {"version": 3}}}, trace
        )
        assert score == 0.0


class TestStepEfficiency:
    def test_returns_none_without_expectation(self):
        assert step_efficiency({}, _trace([])) is None

    def test_under_budget(self):
        trace = _trace([_tool_call("a"), _tool_call("b")])
        assert step_efficiency({"max_steps": 5}, trace) == 1.0

    def test_over_budget(self):
        trace = _trace([_tool_call(f"t{i}") for i in range(10)])
        score = step_efficiency({"max_steps": 5}, trace)
        assert 0.0 < score < 1.0


class TestPlanQuality:
    def test_returns_none_without_expectation(self):
        assert plan_quality({}, _trace([])) is None

    def test_routes_within_allowed(self):
        trace = _trace([_route("profile_agent"), _route("matching_agent")])
        score = plan_quality(
            {"expected_routes": ["profile_agent", "matching_agent", "outreach_agent"]},
            trace,
        )
        assert score == 1.0

    def test_route_out_of_allowed(self):
        trace = _trace([_route("rogue_agent")])
        score = plan_quality({"expected_routes": ["profile_agent"]}, trace)
        assert score == 0.0


class TestAuditLogActionTaken:
    def test_returns_none_without_expectation(self):
        assert audit_log_action_taken({}, _trace([])) is None

    def test_action_succeeded(self):
        trace = _trace([_tool_call("save_skills"), _tool_result("save_skills", "ok")])
        score = audit_log_action_taken({"expected_actions": ["save_skills"]}, trace)
        assert score == 1.0

    def test_action_failed(self):
        trace = _trace([_tool_call("save_skills"), _tool_result("save_skills", "error")])
        score = audit_log_action_taken({"expected_actions": ["save_skills"]}, trace)
        assert score == 0.0


class TestCardFormatCorrectness:
    def test_returns_none_without_expectation(self):
        assert card_format_correctness({}, {}) is None

    def test_artifact_present_with_schema(self):
        score = card_format_correctness(
            {"expected_artifacts": {"matched_jobs": "hr-agent/JobCard@v1"}},
            {"matched_jobs": {"schema": "hr-agent/JobCard@v1", "items": []}},
        )
        assert score == 1.0

    def test_artifact_missing(self):
        score = card_format_correctness(
            {"expected_artifacts": {"matched_jobs": "hr-agent/JobCard@v1"}}, {}
        )
        assert score == 0.0
