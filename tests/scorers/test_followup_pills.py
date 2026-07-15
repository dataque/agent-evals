"""#25 Follow-up Pills Correctness — the pill set is read from the NEXT_STEPS
CUSTOM event (pills refactor: the emit_followups tool is gone and the scenario
id does not ride the wire). Legacy captures still extract via the fallbacks."""

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


def _next_steps(pills: list[str], seq: int = 1) -> Event:
    return Event(seq=seq, type="CUSTOM",
                 payload={"type": "CUSTOM", "name": "NEXT_STEPS",
                          "value": [{"id": str(i), "suggestion": t} for i, t in enumerate(pills)]})


def test_pills_exact_match():
    run = _run(events=[_next_steps(["Suggest open roles", "What else can you help me with?"])])
    s = FollowupPillsCorrectness().score(_ctx(
        run, expected_pills=["Suggest open roles", "What else can you help me with?"]))
    assert s.value == 1.0 and s.passed
    assert s.details["next_steps_event_count"] == 1


def test_pills_order_insensitive():
    run = _run(events=[_next_steps(["Suggest open roles", "Suggest improvements to my skills"])])
    s = FollowupPillsCorrectness().score(_ctx(
        run, expected_pills=["Suggest improvements to my skills", "Suggest open roles"]))
    assert s.value == 1.0


def test_missing_pill_fails():
    run = _run(events=[_next_steps(["Suggest open roles"])])
    s = FollowupPillsCorrectness().score(_ctx(
        run, expected_pills=["Suggest open roles", "How can I apply to a role?"]))
    assert s.value < 1.0 and not s.passed
    assert s.details["pills_missing"] == ["How can I apply to a role?"]


def test_scenario_id_is_derivational_metadata_only():
    # expected_scenario_id rides along for traceability but is never scored —
    # the NEXT_STEPS wire has no scenario id.
    run = _run(events=[_next_steps(["Please save these skills to my profile"])])
    s = FollowupPillsCorrectness().score(_ctx(
        run, expected_scenario_id="SkillsEdited",
        expected_pills=["Please save these skills to my profile"]))
    assert s.value == 1.0
    assert s.details["expected_scenario_id"] == "SkillsEdited"
    assert s.details["observed_scenario_id"] is None


def test_scenario_id_only_skips():
    run = _run(events=[_next_steps(["Suggest open roles"])])
    s = FollowupPillsCorrectness().score(_ctx(run, expected_scenario_id="ColdStart"))
    assert s.skipped


def test_double_emit_flagged_and_last_emission_scored():
    run = _run(events=[
        _next_steps(["Suggest open roles"], seq=1),
        _next_steps(["What else can you help me with?"], seq=2),
    ])
    s = FollowupPillsCorrectness().score(_ctx(
        run, expected_pills=["What else can you help me with?"]))
    assert s.value == 1.0
    assert s.details["next_steps_event_count"] == 2
    assert s.details["double_emit"] is True


def test_legacy_emit_followups_capture_still_extracts():
    run = _run(tool_calls=[ToolCall(
        tool_call_id="e1", name="emit_followups",
        result={"scenarioId": "cold_start",
                "pills": [{"id": "1", "text": "Update my skills"}]},
    )])
    s = FollowupPillsCorrectness().score(_ctx(run, expected_pills=["Update my skills"]))
    assert s.value == 1.0
    assert s.details["next_steps_event_count"] == 0


def test_skips_without_expectations():
    run = _run(events=[_next_steps(["x"])])
    assert FollowupPillsCorrectness().score(_ctx(run)).skipped
