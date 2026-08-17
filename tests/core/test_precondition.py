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


def test_never_derived_fact_is_scored_not_skipped():
    """A fact no tool produced means the agent probably never acted (E8).

    That is a behavioural failure and must reach the scorers, unlike a fact that
    was derived and came back False, which is a real environment condition.
    """
    case = _case("needs-matches", requires=["has_matched_requisitions"])
    report = _runner([_OkScorer()], facts={}).run([case], run_name="t")
    md = report.case_results[0].metadata
    assert md.get("precondition_skipped") is None, "must NOT be skipped"
    assert md["precondition_never_derived"] == ["has_matched_requisitions"]
    assert report.aggregates["cases.scored"] == 1.0
    assert report.aggregates["cases.precondition_never_derived"] == 1.0
    assert "cases.skipped_precondition" not in report.aggregates
    assert report.aggregates["ok.mean"] == 1.0  # the scorers actually ran


def test_derived_false_still_skips():
    """The other half of the split must keep its old behaviour."""
    report = _runner([_OkScorer()], facts={"has_matched_requisitions": False}).run(
        [_case("needs-matches", requires=["has_matched_requisitions"])], run_name="t")
    md = report.case_results[0].metadata
    assert md["precondition_skipped"] is True
    assert "precondition_never_derived" not in md
    assert report.aggregates["cases.skipped_precondition"] == 1.0
    assert "cases.precondition_never_derived" not in report.aggregates


def test_contradiction_is_asserted_against_run_wide_evidence():
    """If one case derived a fact true, the environment supports it, so another
    case that failed to derive it is the agent's doing (E8's skip-pair tell)."""

    class _SplitDriver:
        def __init__(self, case): self.case = case
        def ask(self, question):
            return RunRecord(thread_id="t", run_id="r", assistant_text="ok")

    # 'proves' derives the fact true; 'masked' derives nothing at all
    facts_by_case = {"proves": {"has_matched_requisitions": True}, "masked": {}}
    holder = {}

    def derive(runs):
        return facts_by_case[holder["current"]]

    class _TrackingRunner(Runner):
        def _drive_case(self, case):
            holder["current"] = case.id
            return super()._drive_case(case)

    runner = _TrackingRunner(session_factory=_SplitDriver, scorers=[_OkScorer()],
                             sink=_NullSink(), config={"derive_facts": derive})
    report = runner.run([_case("proves", requires=["has_matched_requisitions"]),
                         _case("masked", requires=["has_matched_requisitions"])], run_name="t")

    contradictions = report.aggregates["precondition_contradictions"]
    assert [c["case_id"] for c in contradictions] == ["masked"]
    assert contradictions[0]["fact"] == "has_matched_requisitions"


def test_errored_scorer_is_visible_not_silent():
    report = _runner([_BoomScorer()]).run([_case("c1")], run_name="t")
    assert report.aggregates["boom.errors"] == 1.0     # surfaced, not dropped
    assert "boom.mean" not in report.aggregates


class _HalfScorer:
    spec = ScorerSpec(metric="half", number=97, title="half", family=Family.JUDGE)
    def score(self, ctx): return Score(metric="half", value=0.6).with_threshold(0.9)  # default → fail


def test_config_threshold_overrides_pass_fail():
    # default threshold 0.9 → 0.6 fails
    r1 = Runner(session_factory=_Driver, scorers=[_HalfScorer()], sink=_NullSink(), config={}).run(
        [_case("c")], run_name="t")
    assert r1.case_results[0].scores[0].passed is False
    # calibration threshold 0.5 → 0.6 now passes (#28)
    r2 = Runner(session_factory=_Driver, scorers=[_HalfScorer()], sink=_NullSink(),
                config={"thresholds": {"half": 0.5}}).run([_case("c")], run_name="t")
    assert r2.case_results[0].scores[0].passed is True
    assert r2.case_results[0].scores[0].threshold == 0.5
