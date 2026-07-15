"""HR per-run fact derivation — applicability/branch facts read from the tool
results / session state of each run, so the eval is portable across
environments. Post pills-refactor wire: skills ride session state (the skills
tools return only an ack) and profile completeness comes from the
analyze_talent_profile result (the old pill scenarios are gone)."""

from __future__ import annotations

from agent_evals.core.run_record import RunRecord, ToolCall
from agent_evals.datasets.facts import derive_hr_facts

_ACK = {"status": "SUCCESS", "data": {"result": "State property skills updated."}}


def _run(*tool_calls, state=None) -> RunRecord:
    return RunRecord(thread_id="t", run_id="r", tool_calls=list(tool_calls), final_state=state)


def _tc(name, result):
    return ToolCall(tool_call_id=name, name=name, result=result)


def test_matches_present_and_absent():
    has = derive_hr_facts([_run(_tc("suggest_requisitions", {"matches": [{"requisition": {}}], "count": 1}))])
    assert has["has_matched_requisitions"] is True and has["no_matched_requisitions"] is False
    none = derive_hr_facts([_run(_tc("suggest_requisitions", {"matches": [], "count": 0}))])
    assert none["has_matched_requisitions"] is False and none["no_matched_requisitions"] is True


def test_skills_read_from_session_state():
    f = derive_hr_facts([_run(
        _tc("get_skills", _ACK),
        state={"skills": {"top": [{"name": "Python"}], "additional": []}},
    )])
    assert f["has_skills"] is True
    empty = derive_hr_facts([_run(
        _tc("get_skills", _ACK),
        state={"skills": {"top": [], "additional": []}},
    )])
    assert empty["has_skills"] is False


def test_suggest_skills_state_is_not_saved_skills_evidence():
    # suggest/edit_skills overwrite state.skills with INFERRED/edited skills —
    # only a get_skills run is evidence of what is actually saved.
    f = derive_hr_facts([_run(
        _tc("suggest_skills", _ACK),
        state={"skills": {"top": [{"name": "Inferred"}], "additional": []}},
    )])
    assert "has_skills" not in f


def test_profile_completeness_from_analysis_result():
    complete = derive_hr_facts([_run(_tc("analyze_talent_profile", {
        "talentProfile": {}, "missingSections": [], "nextActions": [], "profileStrength": 100}))])
    assert complete["profile_complete"] is True and complete["profile_sparse"] is False
    sparse = derive_hr_facts([_run(_tc("analyze_talent_profile", {
        "talentProfile": {}, "missingSections": ["skills", "experience", "education", "languages"],
        "nextActions": [], "profileStrength": 20}))])
    assert sparse["profile_complete"] is False and sparse["profile_sparse"] is True


def test_absent_tool_means_absent_fact():
    # no requisition tool ran this turn → the fact is simply not derivable, so a
    # case that requires it will skip (not silently pass).
    f = derive_hr_facts([_run(_tc("get_skills", _ACK), state={"skills": {"top": []}})])
    assert "has_matched_requisitions" not in f
    assert f["has_skills"] is False
