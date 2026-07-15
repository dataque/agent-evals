"""#3 Tool Argument Correctness — the structural matcher engine (literals,
$-matchers, nested subset patterns) and its scorer integration. Matchers keep
arg goldens data-independent: shape is asserted, env ids are never pinned."""

from __future__ import annotations

from agent_evals.core.case import EvalCase, Expectations
from agent_evals.core.run_record import RunRecord, ToolCall
from agent_evals.core.scorer import ScoringContext
from agent_evals.scorers import ToolArgumentCorrectness
from agent_evals.scorers.tool_arguments import _MISSING, match_value


def _ctx(run: RunRecord, **exp) -> ScoringContext:
    case = EvalCase(id="t", question="q", expectations=Expectations(**exp))
    return ScoringContext(case=case, runs=[run], turn_index=0)


def _run(*tool_calls) -> RunRecord:
    return RunRecord(thread_id="t", run_id="r", tool_calls=list(tool_calls))


def _tc(name, args=None):
    return ToolCall(tool_call_id=name, name=name, args=args or {})


# ---- engine: literals ------------------------------------------------------
def test_literal_exact_and_eq():
    assert match_value("329727BR", "329727BR")[0]
    ok, why = match_value("329727BR", "111111BR")
    assert not ok and "!=" in why
    assert match_value({"$eq": 5}, 5)[0]
    assert not match_value({"$eq": 5}, 6)[0]


def test_exists():
    assert match_value({"$exists": True}, "x")[0]
    assert not match_value({"$exists": True}, _MISSING)[0]
    assert not match_value({"$exists": True}, None)[0]
    assert match_value({"$exists": False}, _MISSING)[0]
    assert not match_value({"$exists": False}, "x")[0]


def test_type_matrix():
    assert match_value({"$type": "string"}, "s")[0]
    assert match_value({"$type": "array"}, [1])[0]
    assert match_value({"$type": "object"}, {"a": 1})[0]
    assert match_value({"$type": "integer"}, 3)[0]
    assert match_value({"$type": "number"}, 3.5)[0]
    # bool must NOT satisfy integer/number (Python bool is an int subclass)
    assert not match_value({"$type": "integer"}, True)[0]
    assert not match_value({"$type": "number"}, True)[0]
    assert match_value({"$type": "boolean"}, True)[0]
    assert not match_value({"$type": "string"}, 3)[0]


def test_in_regex_size():
    assert match_value({"$in": ["GOOD", "STRONG"]}, "STRONG")[0]
    assert not match_value({"$in": ["GOOD", "STRONG"]}, "HIGH")[0]
    assert match_value({"$regex": "(?i)^java$"}, "Java")[0]
    assert not match_value({"$regex": "(?i)^java$"}, "JavaScript")[0]
    assert not match_value({"$regex": "x"}, 42)[0]           # non-string
    assert not match_value({"$regex": "("}, "x")[0]          # invalid pattern -> fail, not raise
    assert match_value({"$size": {"min": 1, "max": 3}}, [1, 2])[0]
    assert not match_value({"$size": {"min": 3}}, [1, 2])[0]
    assert not match_value({"$size": {"max": 1}}, "ab")[0]
    assert not match_value({"$size": {"min": 1}}, 42)[0]     # unsized -> fail


def test_contains_and_contains_all():
    items = [{"name": "Java", "source": "MANUAL"}, {"name": "React", "source": "MANUAL"}]
    assert match_value({"$contains": {"name": "Java"}}, items)[0]
    assert not match_value({"$contains": {"name": "Go"}}, items)[0]
    assert match_value({"$contains_all": [{"name": "Java"}, {"name": "React"}]}, items)[0]
    ok, why = match_value({"$contains_all": [{"name": "Java"}, {"name": "Go"}]}, items)
    assert not ok and "Go" in why
    assert not match_value({"$contains": {"name": "Java"}}, "not-a-list")[0]
    # matcher inside a contained item's subset pattern
    assert match_value({"$contains": {"name": {"$regex": "(?i)^java$"}, "source": "MANUAL"}}, items)[0]


def test_nested_subset_and_invalid_specs():
    obs = {"filters": {"level": "Director", "location": "London"}, "extra": 1}
    assert match_value({"filters": {"level": "Director"}}, obs)[0]          # extras tolerated
    ok, why = match_value({"filters": {"level": "VP"}}, obs)
    assert not ok and "filters.level" in why
    ok, why = match_value({"filters": {"missing_key": 1}}, obs)
    assert not ok and "missing" in why
    ok, why = match_value({"$type": "string", "name": "x"}, "s")            # mixed keys
    assert not ok and "mixes" in why
    ok, why = match_value({"$bogus": 1}, "s")                               # unknown op
    assert not ok and "unknown matcher" in why


# ---- scorer integration ----------------------------------------------------
def _score(spec, *calls):
    return ToolArgumentCorrectness().score(_ctx(_run(*calls), expected_tool_args=spec))


def test_shape_spec_passes_without_pinning_ids():
    s = _score({"view_requisition": {"requisition": {"$type": "string", "$size": {"min": 1}}}},
               _tc("view_requisition", {"requisition": "ANY-ENV-ID-42"}))
    assert s.value == 1.0 and s.passed


def test_missing_key_fails_with_reason():
    s = _score({"draft_message": {"recruiterId": {"$type": "string"}}},
               _tc("draft_message", {"requisitionId": "R1"}))
    assert s.value == 0.0
    assert "recruiterId" in s.details["failure_reasons"]["draft_message"]


def test_tool_not_called_is_missed():
    s = _score({"edit_skills": {"top": {"$type": "array"}}}, _tc("get_skills", {}))
    assert s.value == 0.0
    assert s.details["failure_reasons"]["edit_skills"] == "tool not called"


def test_empty_spec_means_called_with_any_args():
    s = _score({"save_skills": {}}, _tc("save_skills", {}))
    assert s.value == 1.0


def test_any_candidate_call_may_satisfy():
    s = _score({"view_requisition": {"requisition": {"$regex": "BR$"}}},
               _tc("view_requisition", {"requisition": "nope"}),
               ToolCall(tool_call_id="v2", name="view_requisition", args={"requisition": "329727BR"}))
    assert s.value == 1.0


def test_realistic_edit_skills_spec():
    # run3-verified wire shape: args wrapped under the method-parameter name,
    # with innerThought/confidence side-channels (tolerated by subset match)
    spec = {"edit_skills": {"editSkillsInput": {"top": {
        "$type": "array", "$size": {"min": 5, "max": 10},
        "$contains_all": [
            {"name": "Java", "source": "MANUAL"}, {"name": "React", "source": "MANUAL"},
            {"name": "Python", "source": "MANUAL"}, {"name": "Analytics", "source": "MANUAL"},
            {"name": "P&L", "source": "MANUAL"},
        ]}}}}
    good = [{"name": n, "source": "MANUAL"} for n in ("Java", "React", "Python", "Analytics", "P&L")]
    ok = _score(spec, _tc("edit_skills", {"editSkillsInput": {"top": good, "additional": []},
                                          "innerThought": "staging skills", "confidence": "high"}))
    assert ok.value == 1.0
    # the run3 granular-add defect shape: top replaced with a single item ->
    # $size(min 5) fails and the reason pinpoints it
    s = _score(spec, _tc("edit_skills", {"editSkillsInput": {"top": good[:1], "additional": []}}))
    assert s.value == 0.0 and "size 1 < min 5" in s.details["failure_reasons"]["edit_skills"]


def test_legacy_exact_specs_still_work():
    s = _score({"view_requisition": {"requisition": "329727BR"}},
               _tc("view_requisition", {"requisition": "329727BR", "extra": 1}))
    assert s.value == 1.0
    bad = _score({"view_requisition": {"requisition": "111111BR"}},
                 _tc("view_requisition", {"requisition": "329727BR"}))
    assert bad.value == 0.0
