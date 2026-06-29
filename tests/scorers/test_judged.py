"""Judged scorers exercised with the deterministic HeuristicJudge (no network),
plus judge selection/binding and the error-verdict path."""

from __future__ import annotations

from agent_evals.core.case import EvalCase, Expectations
from agent_evals.core.judge import JudgeVerdict
from agent_evals.core.run_record import RunRecord, ToolCall, ToolStatus
from agent_evals.core.scorer import ScoringContext
from agent_evals.judges import HeuristicJudge, apply_per_metric_judges, build_judge
from agent_evals.scorers import (
    AnswerEquivalence,
    Bias,
    Faithfulness,
    GEval,
    RefusalCorrectness,
    Safety,
    TaskCompletion,
    TokenCost,
    TopicAdherence,
    get_scorers,
)


def _run(**kw) -> RunRecord:
    base = dict(thread_id="t", run_id="r", user_id="A", assistant_text="Here are your skills: Python, Docker.")
    base.update(kw)
    return RunRecord(**base)


def _ctx(run, *, judge=None, config=None, **exp) -> ScoringContext:
    case = EvalCase(id="t", question="What are my skills?", expectations=Expectations(**exp))
    return ScoringContext(case=case, runs=[run], turn_index=0, judge=judge, config=config or {})


def test_answer_equivalence_uses_judge_and_golden():
    j = HeuristicJudge(fixed_score=0.9)
    s = AnswerEquivalence().score(_ctx(_run(), judge=j, expected_response="Python and Docker"))
    assert s.value == 0.9 and s.passed and s.details["judge"] == "heuristic"
    # no golden -> skip
    assert AnswerEquivalence().score(_ctx(_run(), judge=j)).skipped
    # no judge -> skip
    assert AnswerEquivalence().score(_ctx(_run(), expected_response="x")).skipped


def test_per_metric_judge_injection_wins_over_ctx():
    own = HeuristicJudge(fixed_score=0.2)
    other = HeuristicJudge(fixed_score=1.0)
    s = AnswerEquivalence(judge=own).score(_ctx(_run(), judge=other, expected_response="ref"))
    assert s.value == 0.2  # scorer's own judge wins


def test_faithfulness_needs_tool_context():
    j = HeuristicJudge(fixed_score=1.0)
    # no tool outputs -> skip
    assert Faithfulness().score(_ctx(_run(), judge=j)).skipped
    grounded = _run(tool_calls=[ToolCall(tool_call_id="c", name="get_skills",
                                         result={"skills": ["Python"]}, status=ToolStatus.OK)])
    assert Faithfulness().score(_ctx(grounded, judge=j)).value == 1.0


def test_faithfulness_skips_must_refuse():
    j = HeuristicJudge(fixed_score=1.0)
    grounded = _run(tool_calls=[ToolCall(tool_call_id="c", name="Task", result={"x": 1}, status=ToolStatus.OK)])
    assert Faithfulness().score(_ctx(grounded, judge=j, must_refuse=True)).skipped


def test_safety_forbidden_hard_fail_without_judge():
    run = _run(assistant_text="Their salary is 200k")
    s = Safety().score(_ctx(run, forbidden_substrings=["salary"]))
    assert s.value == 0.0 and s.passed is False
    # no forbidden, no judge -> safe by default
    assert Safety().score(_ctx(_run())).value == 1.0
    # pii surfaced as evidence but judge decides (here fixed safe)
    pii_run = _run(assistant_text="email me at a@b.com")
    s2 = Safety().score(_ctx(pii_run, judge=HeuristicJudge(fixed_score=1.0)))
    assert s2.value == 1.0 and s2.details["pii_findings"].get("email") == 1


def test_refusal_only_when_flagged():
    j = HeuristicJudge(fixed_score=1.0)
    assert RefusalCorrectness().score(_ctx(_run(), judge=j)).skipped  # no must_refuse
    s = RefusalCorrectness().score(_ctx(_run(assistant_text="I can't help with that, but I can help with your career."),
                                        judge=j, must_refuse=True, expected_redirect="career topics"))
    assert s.value == 1.0


def test_geval_requires_rubric():
    j = HeuristicJudge(fixed_score=0.7)
    assert GEval().score(_ctx(_run(), judge=j)).skipped
    s = GEval().score(_ctx(_run(), judge=j, rubric="Is the response concise and professional?"))
    assert s.value == 0.7


def test_reference_free_judges_skip_on_empty_text():
    j = HeuristicJudge(fixed_score=0.8)
    for scorer in (TopicAdherence(), Bias(), TaskCompletion()):
        assert scorer.score(_ctx(_run(assistant_text=""), judge=j)).skipped or scorer.spec.metric == "task_completion"
    # with text, topic/bias produce a value
    assert TopicAdherence().score(_ctx(_run(), judge=j)).value == 0.8


def test_task_completion_skips_must_refuse():
    # a correct refusal isn't "task completion" — refusal_correctness (#9) owns it
    j = HeuristicJudge(fixed_score=1.0)
    s = TaskCompletion().score(_ctx(_run(assistant_text="I can't help with that."),
                                    judge=j, must_refuse=True))
    assert s.skipped and "refus" in (s.skip_reason or "").lower()


def test_token_cost_is_operational():
    s = TokenCost().score(_ctx(_run()))
    assert s.value is None and s.details["source"] in ("estimated", "reported", "unknown")


def test_error_verdict_marks_score_errored_not_zero():
    class BoomJudge:
        name = "boom"

        def evaluate(self, **kw):
            return JudgeVerdict(score=0.0, passed=None, rationale="x", raw={"error": "kaboom"})

    s = Bias(judge=BoomJudge()).score(_ctx(_run()))
    assert s.error == "kaboom" and s.value is None  # excluded from aggregation


def test_build_judge_and_apply_per_metric():
    assert build_judge("heuristic").name == "heuristic"
    assert build_judge("azure").name == "azure_openai"
    scorers = get_scorers("judge")
    apply_per_metric_judges(scorers, default="heuristic",
                            per_metric={"faithfulness": "heuristic"})
    # every judged scorer now has a judge bound
    assert all(getattr(s, "judge", None) is not None for s in scorers)
