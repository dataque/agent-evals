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


def test_no_has_skills_fact_is_derived():
    """`has_skills` is deliberately gone (E10).

    It was only ever derivable from a `get_skills` run, because suggest_skills
    and edit_skills overwrite the same state property with inferred or staged
    skills rather than saved ones. `get_skills` was removed from the product, so
    the fact can never be true again and any `requires: has_skills` would skip
    unconditionally. Deriving it as False would be worse than not deriving it:
    False reads as "the environment has no skills", which is not what is known.
    """
    saved_shape = derive_hr_facts([_run(
        _tc("get_skills", _ACK),
        state={"skills": {"top": [{"name": "Python"}], "additional": []}},
    )])
    assert "has_skills" not in saved_shape

    inferred = derive_hr_facts([_run(
        _tc("suggest_skills", _ACK),
        state={"skills": {"top": [{"name": "Inferred"}], "additional": []}},
    )])
    assert "has_skills" not in inferred


def test_profile_completeness_from_analysis_result():
    complete = derive_hr_facts([_run(_tc("analyze_talent_profile", {
        "talentProfile": {}, "missingSections": [], "nextActions": [], "profileStrength": 100}))])
    assert complete["profile_complete"] is True and complete["profile_sparse"] is False
    sparse = derive_hr_facts([_run(_tc("analyze_talent_profile", {
        "talentProfile": {}, "missingSections": ["skills", "experience", "education", "languages"],
        "nextActions": [], "profileStrength": 20}))])
    assert sparse["profile_complete"] is False and sparse["profile_sparse"] is True


def test_absent_tool_means_absent_fact():
    # No requisition tool ran this turn, so the fact is simply not derivable.
    # The runner distinguishes that from a fact derived False: absent means the
    # agent never looked, which is scored as a failure rather than skipped (E8).
    f = derive_hr_facts([_run(_tc("suggest_skills", _ACK))])
    assert "has_matched_requisitions" not in f
    assert "no_matched_requisitions" not in f
