"""Unit tests for the Level-1 deterministic + operational + probe scorers."""

from __future__ import annotations

import math

from agent_evals.core.case import EvalCase, Expectations
from agent_evals.core.run_record import (
    DerivedTiming,
    RunRecord,
    StreamHealth,
    ToolCall,
    ToolStatus,
)
from agent_evals.core.scorer import ScoringContext
from agent_evals.scorers import (
    AuditLogActionTaken,
    CrossUserIsolation,
    Latency,
    StepEfficiency,
    StreamHealthDetail,
    StringCheck,
    ToolArgumentCorrectness,
    ToolResultSchemaAdherence,
    ToolSelectionAccuracy,
)


def _tc(name, args=None, result=None, status=ToolStatus.OK, is_error=False):
    return ToolCall(tool_call_id=name, name=name, args=args or {}, result=result,
                    status=status, is_error=is_error)


def _ctx(run: RunRecord, **exp) -> ScoringContext:
    case = EvalCase(id="t", question="q", expectations=Expectations(**exp))
    return ScoringContext(case=case, runs=[run], turn_index=0)


def _run(**kw) -> RunRecord:
    base = dict(thread_id="t", run_id="r", user_id="A")
    base.update(kw)
    return RunRecord(**base)


# ---- #2 tool selection ----------------------------------------------------
def test_tool_selection_f1():
    run = _run(tool_calls=[_tc("suggest_skills"), _tc("get_skills")])
    s = ToolSelectionAccuracy().score(_ctx(run, expected_tool_calls=["suggest_skills"]))
    assert math.isclose(s.value, 2 * 0.5 * 1.0 / 1.5)  # precision .5, recall 1
    assert s.details["unexpected"] == ["get_skills"]
    perfect = ToolSelectionAccuracy().score(
        _ctx(run, expected_tool_calls=["suggest_skills", "get_skills"]))
    assert perfect.value == 1.0 and perfect.passed
    assert ToolSelectionAccuracy().score(_ctx(run)).skipped  # no golden


# ---- #3 tool arguments ----------------------------------------------------
def test_tool_arguments_subset_match():
    run = _run(tool_calls=[_tc("view_requisition", args={"requisition": "329727BR", "extra": 1})])
    ok = ToolArgumentCorrectness().score(_ctx(run, expected_tool_args={"view_requisition": {"requisition": "329727BR"}}))
    assert ok.value == 1.0
    bad = ToolArgumentCorrectness().score(_ctx(run, expected_tool_args={"view_requisition": {"requisition": "111111BR"}}))
    assert bad.value == 0.0


# ---- #4 tool result schema ------------------------------------------------
_ACK = {"status": "SUCCESS", "data": {"result": "State property skills updated."}}
_SKILLS_STATE = {"skills": {"top": [{"name": "Python", "source": "AI_INFERRED"}], "additional": []}}


def test_tool_result_schema_adherence():
    # skills tools: the ack envelope on the result AND the `skills` state
    # property must both validate (2 checks)
    valid = _run(tool_calls=[_tc("suggest_skills", result=_ACK)], final_state=_SKILLS_STATE)
    assert ToolResultSchemaAdherence().score(_ctx(valid)).value == 1.0
    # ack fine but no state captured -> the state check fails
    no_state = _run(tool_calls=[_tc("suggest_skills", result=_ACK)])
    s = ToolResultSchemaAdherence().score(_ctx(no_state))
    assert s.value == 0.5 and any("state" in str(f) for f in s.details["failures"])
    # pre-refactor payload shape on the result -> the ack check fails
    old_shape = _run(tool_calls=[_tc("suggest_skills", result={"top": [], "additional": []})],
                     final_state=_SKILLS_STATE)
    s2 = ToolResultSchemaAdherence().score(_ctx(old_shape))
    assert s2.value == 0.5
    # a non-state tool validates its result only
    bare = _run(tool_calls=[_tc("answer_requisition_questions",
                                result={"requisitionContainsAnswer": True, "answer": "Yes"})])
    assert ToolResultSchemaAdherence().score(_ctx(bare)).value == 1.0
    # tool with no registered schema -> skipped
    assert ToolResultSchemaAdherence().score(_ctx(_run(tool_calls=[_tc("unregistered_tool", result={})]))).skipped


# ---- #16 audit / action taken ---------------------------------------------
def test_audit_action_taken():
    run = _run(tool_calls=[_tc("save_skills", status=ToolStatus.OK)])
    assert AuditLogActionTaken().score(_ctx(run, expected_actions=["save_skills"])).value == 1.0
    failed = _run(tool_calls=[_tc("save_skills", status=ToolStatus.MISSING_RESULT)])
    assert AuditLogActionTaken().score(_ctx(failed, expected_actions=["save_skills"])).value == 0.0


# ---- #18 step efficiency --------------------------------------------------
def test_step_efficiency():
    run = _run(tool_calls=[_tc("a"), _tc("b"), _tc("c")])
    assert StepEfficiency().score(_ctx(run, max_steps=5)).value == 1.0
    assert math.isclose(StepEfficiency().score(_ctx(run, max_steps=2)).value, 2 / 3)


# ---- #22 string check -----------------------------------------------------
def test_string_check():
    run = _run(assistant_text="Here are your Python and Docker skills.")
    s = StringCheck().score(_ctx(run, response_must_contain=["Python", "Docker"],
                                 forbidden_substrings=["salary"]))
    assert s.value == 1.0 and s.passed
    s2 = StringCheck().score(_ctx(run, response_must_contain=["Python", "Kubernetes"]))
    assert s2.value == 0.5 and s2.details["missing"] == ["Kubernetes"]


# ---- #13 latency (operational) --------------------------------------------
def test_latency_operational_and_sla():
    run = _run(timing=DerivedTiming(ttft_ms=120.0, total_ms=1800.0))
    no_sla = Latency().score(_ctx(run))
    assert no_sla.value is None and no_sla.details["total_ms"] == 1800.0
    ctx = _ctx(run)
    ctx.config = {"latency_total_sla_ms": 1000}
    assert Latency().score(ctx).value == 0.0  # 1800 > 1000


# ---- #24 stream health ----------------------------------------------------
def test_stream_health():
    clean = _run(stream_health=StreamHealth(run_started_seen=True, run_finished_seen=True))
    assert StreamHealthDetail().score(_ctx(clean)).value == 1.0
    dirty = _run(stream_health=StreamHealth(run_started_seen=True, run_finished_seen=True,
                                            malformed_arg_tool_calls=["x"]))
    s = StreamHealthDetail().score(_ctx(dirty))
    assert s.value == 0.9 and "args_well_formed" in s.details["failed"]


# ---- #8 cross-user isolation ----------------------------------------------
def test_isolation_probe():
    leak = _run(user_id="A", assistant_text="Here is B's profile",
                tool_calls=[_tc("get_talent_profile", result={"userId": "B", "name": "Bob"})])
    s = CrossUserIsolation().score(_ctx(leak, other_user_id="B"))
    assert s.value == 0.0 and s.details["leaked"]
    assert "B" in s.details["foreign_id_candidates"]
    clean = _run(user_id="A", assistant_text="Here is your profile",
                 tool_calls=[_tc("get_talent_profile", result={"userId": "A"})])
    assert CrossUserIsolation().score(_ctx(clean, other_user_id="B")).value == 1.0
    # naming the requested id while REFUSING (no tool returned their data) is NOT a leak
    refusal = _run(user_id="A", assistant_text="I can't show the profile for employee B.")
    assert CrossUserIsolation().score(_ctx(refusal, other_user_id="B")).value == 1.0


# ---- #2 optional_tool_calls (non-deterministic benign reads) --------------
def test_tool_selection_optional_calls_not_penalized():
    # a pre-edit get_skills read is neither required nor penalized
    read = _run(tool_calls=[_tc("get_skills"), _tc("edit_skills")])
    s = ToolSelectionAccuracy().score(_ctx(read, expected_tool_calls=["edit_skills"],
                                           optional_tool_calls=["get_skills"]))
    assert s.value == 1.0, s.details
    # and its absence is fine too
    noread = _run(tool_calls=[_tc("edit_skills")])
    assert ToolSelectionAccuracy().score(_ctx(noread, expected_tool_calls=["edit_skills"],
                                              optional_tool_calls=["get_skills"])).value == 1.0
    # a genuinely wrong tool is still penalized even with an optional set
    wrong = _run(tool_calls=[_tc("edit_skills"), _tc("save_skills")])
    assert ToolSelectionAccuracy().score(_ctx(wrong, expected_tool_calls=["edit_skills"],
                                              optional_tool_calls=["get_skills"])).value < 1.0
