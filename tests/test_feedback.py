"""#23 User feedback: aggregation, the in-run scorer, ingest pipeline, and the
judge-comparison helper."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from agent_evals.core.case import EvalCase
from agent_evals.core.judge import Judge, JudgeVerdict
from agent_evals.core.scorer import ScoringContext
from agent_evals.feedback import aggregate_feedback, ingest
from agent_evals.judges import HeuristicJudge, compare_judges
from agent_evals.scorers.user_feedback import UserFeedbackSignal
from agent_evals.sinks import JsonlSink


def _run():
    from agent_evals.core.run_record import RunRecord
    return RunRecord(thread_id="t", run_id="r", user_message="q", assistant_text="a")


def test_aggregate_feedback():
    agg = aggregate_feedback([{"thumbs": "up"}, {"thumbs": "down"},
                              {"rating": 0.8, "correction": "tighten the wording"}])
    assert agg["user_feedback.n"] == 3.0
    assert abs(agg["user_feedback.mean"] - (1.0 + 0.0 + 0.8) / 3) < 1e-9
    assert agg["user_feedback.thumbs_up_rate"] == 2 / 3   # 1.0 and 0.8 are >= 0.5
    assert abs(agg["user_feedback.correction_rate"] - 1 / 3) < 1e-9


def test_user_feedback_scorer_reads_case_metadata():
    case = EvalCase(id="c", question="q", metadata={"user_feedback": {"thumbs": "up"}})
    s = UserFeedbackSignal().score(ScoringContext(case=case, runs=[_run()], turn_index=0))
    assert s.value == 1.0 and s.passed
    # absent -> skip
    bare = EvalCase(id="c", question="q")
    assert UserFeedbackSignal().score(ScoringContext(case=bare, runs=[_run()], turn_index=0)).skipped


def test_ingest_writes_summary():
    with tempfile.TemporaryDirectory() as tmp:
        agg = ingest([{"thumbs": "up"}, {"rating": 0.4}], JsonlSink(out_dir=tmp), run_name="fb")
        summary = json.loads((Path(tmp) / "fb" / "summary.json").read_text())
        assert summary["user_feedback.n"] == 2.0
        assert "user_feedback.thumbs_up_rate" in agg


def test_compare_judges_distinct_backends():
    class _J2:
        name = "j2"

        def evaluate(self, **kw) -> JudgeVerdict:
            return JudgeVerdict(score=0.25, passed=False, rationale="meh")

    verdicts = compare_judges([HeuristicJudge(fixed_score=0.9), _J2()],
                              criteria="is it good", response="some answer")
    assert set(verdicts) == {"heuristic", "j2"}
    assert verdicts["heuristic"].score == 0.9 and verdicts["j2"].score == 0.25
