"""Runner precondition filtering (#31) and visible skip/error aggregation.

A case whose profile-state precondition is unmet is skipped WITH a reason and
reported in the summary (never silently dropped — the bug that hid 9 judge
metrics). A scorer that errors surfaces an error count, not silence."""

from __future__ import annotations

from agent_evals.core.case import EvalCase, Expectations
from agent_evals.core.run_record import RunRecord
from agent_evals.core.runner import Runner
from agent_evals.core.scorer import Family, Score, ScorerSpec


class _NullSink:
    def start_run(self, *, name, params): ...
    def log_case_result(self, result, runs): ...
    def log_summary(self, aggregates): ...
    def end_run(self): ...


class _Driver:
    def __init__(self, case): self.case = case
    def ask(self, question: str) -> RunRecord:
        return RunRecord(thread_id="t", run_id="r", assistant_text="ok")


class _OkScorer:
    spec = ScorerSpec(metric="ok", number=99, title="ok", family=Family.DETERMINISTIC)
    def score(self, ctx): return Score(metric="ok", value=1.0).with_threshold(1.0)


class _BoomScorer:
    spec = ScorerSpec(metric="boom", number=98, title="boom", family=Family.JUDGE)
    def score(self, ctx): raise RuntimeError("judge exploded")


def _runner(scorers, profile_state=None) -> Runner:
    config = {"profile_state": profile_state} if profile_state is not None else {}
    return Runner(session_factory=_Driver, scorers=scorers, sink=_NullSink(), config=config)


def _case(cid, requires=None) -> EvalCase:
    return EvalCase(id=cid, question="q", expectations=Expectations(), requires=requires)


def test_precondition_skips_when_state_unmet():
    cases = [_case("needs-matches", requires=["has_matched_requisitions"]), _case("always")]
    report = _runner([_OkScorer()], profile_state={"has_matched_requisitions": False}).run(cases, run_name="t")
    by = {cr.case_id: cr for cr in report.case_results}
    assert by["needs-matches"].metadata.get("precondition_skipped") is True
    assert by["needs-matches"].scores[0].skipped and by["needs-matches"].scores[0].skip_reason
    assert by["always"].metadata.get("precondition_skipped") is None     # ran normally
    assert report.aggregates["cases.total"] == 2.0
    assert report.aggregates["cases.scored"] == 1.0
    assert report.aggregates["cases.skipped_precondition"] == 1.0
    assert report.aggregates["ok.mean"] == 1.0                            # only the scored case


def test_no_filtering_without_profile_state():
    # the transcript-capture run sets no profile_state -> every case runs
    cases = [_case("needs-matches", requires=["has_matched_requisitions"])]
    report = _runner([_OkScorer()]).run(cases, run_name="t")
    assert report.case_results[0].metadata.get("precondition_skipped") is None
    assert report.aggregates["ok.mean"] == 1.0


def test_state_met_runs_case():
    cases = [_case("needs-matches", requires=["has_matched_requisitions"])]
    report = _runner([_OkScorer()], profile_state={"has_matched_requisitions": True}).run(cases, run_name="t")
    assert report.case_results[0].metadata.get("precondition_skipped") is None
    assert report.aggregates["ok.mean"] == 1.0


def test_errored_scorer_is_visible_not_silent():
    report = _runner([_BoomScorer()]).run([_case("c1")], run_name="t")
    assert report.aggregates["boom.errors"] == 1.0     # surfaced, not dropped
    assert "boom.mean" not in report.aggregates
