"""Runner precondition filtering (#31/#32) and visible skip/error aggregation.

Applicability is decided from data-facts DERIVED from each run (so the eval is
portable across dev/UAT/prod with no frozen data), and a skipped case is reported
WITH its reason in the summary — never silently dropped."""

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


def _runner(scorers, facts=None) -> Runner:
    # facts=None → no deriver wired (capture mode, run everything);
    # facts={...} → a deriver returning those run-derived facts.
    config = {"derive_facts": (lambda runs: facts)} if facts is not None else {}
    return Runner(session_factory=_Driver, scorers=scorers, sink=_NullSink(), config=config)


def _case(cid, requires=None) -> EvalCase:
    return EvalCase(id=cid, question="q", expectations=Expectations(), requires=requires)


def test_skips_when_derived_fact_unmet_and_reports_reason():
    cases = [_case("needs-matches", requires=["has_matched_requisitions"]), _case("always")]
    report = _runner([_OkScorer()], facts={"has_matched_requisitions": False}).run(cases, run_name="t")
    by = {cr.case_id: cr for cr in report.case_results}
    assert by["needs-matches"].metadata["precondition_skipped"] is True
    assert by["needs-matches"].metadata["skip_reason"]
    assert by["needs-matches"].metadata["derived_facts"] == {"has_matched_requisitions": False}
    assert by["always"].metadata.get("precondition_skipped") is None         # ran normally
    assert report.aggregates["cases.total"] == 2.0
    assert report.aggregates["cases.scored"] == 1.0
    assert report.aggregates["cases.skipped_precondition"] == 1.0
    assert report.aggregates["ok.mean"] == 1.0                               # only the scored case
    # the skip + reason is in the SUMMARY (the explicit requirement)
    sk = report.aggregates["skipped_cases"]
    assert sk[0]["case_id"] == "needs-matches"
    assert sk[0]["requires"] == ["has_matched_requisitions"] and sk[0]["reason"]


def test_runs_case_when_fact_met():
    report = _runner([_OkScorer()], facts={"has_matched_requisitions": True}).run(
        [_case("needs-matches", requires=["has_matched_requisitions"])], run_name="t")
    assert report.case_results[0].metadata.get("precondition_skipped") is None
    assert report.aggregates["ok.mean"] == 1.0


def test_no_deriver_runs_everything():
    # a pure capture (no derive_facts wired) filters nothing — every case runs
    report = _runner([_OkScorer()]).run(
        [_case("needs-matches", requires=["has_matched_requisitions"])], run_name="t")
    assert report.case_results[0].metadata.get("precondition_skipped") is None
    assert report.aggregates["ok.mean"] == 1.0


def test_errored_scorer_is_visible_not_silent():
    report = _runner([_BoomScorer()]).run([_case("c1")], run_name="t")
    assert report.aggregates["boom.errors"] == 1.0     # surfaced, not dropped
    assert "boom.mean" not in report.aggregates
