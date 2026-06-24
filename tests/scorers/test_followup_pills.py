"""#25 Follow-up Pills Correctness — scenario_id + exact pill set, read from the
emit_followups tool result or a server-side CUSTOM event."""

from __future__ import annotations

from agent_evals.core.case import EvalCase, Expectations
from agent_evals.core.run_record import Event, RunRecord, ToolCall
from agent_evals.core.scorer import ScoringContext
from agent_evals.scorers import FollowupPillsCorrectness


def _ctx(run: RunRecord, **exp) -> ScoringContext:
    case = EvalCase(id="t", question="q", expectations=Expectations(**exp))
    return ScoringContext(case=case, runs=[run], turn_index=0)


def _run(**kw) -> RunRecord:
    return RunRecord(thread_id="t", run_id="r", **kw)


def _emit(scenario: str, pills: list[str]) -> ToolCall:
    return ToolCall(
        tool_call_id="e1", name="emit_followups",
        result={"scenarioId": scenario, "pills": [{"id": str(i), "text": t} for i, t in enumerate(pills)]},
    )


def test_pills_exact_match():
    run = _run(tool_calls=[_emit("profile_analyzed", ["Find matching roles", "Update my skills"])])
    s = FollowupPillsCorrectness().score(_ctx(
        run, expected_scenario_id="profile_analyzed",
        expected_pills=["Find matching roles", "Update my skills"]))
    assert s.value == 1.0 and s.passed


def test_pills_order_insensitive():
    run = _run(tool_calls=[_emit("profile_analyzed", ["Update my skills", "Find matching roles"])])
    s = FollowupPillsCorrectness().score(_ctx(
        run, expected_pills=["Find matching roles", "Update my skills"]))
    assert s.value == 1.0


def test_wrong_scenario_and_missing_pill():
    run = _run(tool_calls=[_emit("no_matches", ["Update my skills"])])
    s = FollowupPillsCorrectness().score(_ctx(
        run, expected_scenario_id="profile_analyzed",
        expected_pills=["Find matching roles", "Update my skills"]))
    assert s.value < 1.0 and not s.passed
    assert s.details["scenario_ok"] is False
    assert s.details["pills_missing"] == ["Find matching roles"]


def test_pills_from_server_side_custom_event():
    run = _run(events=[Event(seq=1, type="CUSTOM",
                             payload={"scenarioId": "cold_start", "pills": ["Update my skills"]})])
    s = FollowupPillsCorrectness().score(_ctx(run, expected_scenario_id="cold_start"))
    assert s.value == 1.0
    assert s.details["observed_scenario_id"] == "cold_start"


def test_skips_without_expectations():
    run = _run(tool_calls=[_emit("x", ["y"])])
    assert FollowupPillsCorrectness().score(_ctx(run)).skipped
