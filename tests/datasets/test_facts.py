"""HR per-run fact derivation — applicability/branch facts read from the tool
results of each run, so the eval is portable across environments."""

from __future__ import annotations

from agent_evals.core.run_record import RunRecord, ToolCall
from agent_evals.datasets.facts import derive_hr_facts


def _run(*tool_calls) -> RunRecord:
    return RunRecord(thread_id="t", run_id="r", tool_calls=list(tool_calls))


def _tc(name, result):
    return ToolCall(tool_call_id=name, name=name, result=result)


def test_matches_present_and_absent():
    has = derive_hr_facts([_run(_tc("suggest_requisitions", {"matches": [{"requisition": {}}]}))])
    assert has["has_matched_requisitions"] is True and has["no_matched_requisitions"] is False
    none = derive_hr_facts([_run(_tc("suggest_requisitions", {"matches": []}))])
    assert none["has_matched_requisitions"] is False and none["no_matched_requisitions"] is True


def test_skills_and_profile_scenario():
    f = derive_hr_facts([_run(
        _tc("get_skills", {"top": [{"name": "Python"}], "additional": []}),
        _tc("emit_followups", {"scenarioId": "profile_analyzed", "pills": []}),
    )])
    assert f["has_skills"] is True
    assert f["profile_complete"] is True and f["profile_sparse"] is False


def test_absent_tool_means_absent_fact():
    # no requisition tool ran this turn → the fact is simply not derivable, so a
    # case that requires it will skip (not silently pass).
    f = derive_hr_facts([_run(_tc("get_skills", {"top": []}))])
    assert "has_matched_requisitions" not in f
    assert f["has_skills"] is False
