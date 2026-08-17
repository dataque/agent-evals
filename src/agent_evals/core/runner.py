"""The neutral orchestration spine.

load cases → drive a per-case session to get one RunRecord per turn → run the
applicable scorers → push scores + run artifacts to the sink → aggregate.

There is deliberately no ``mlflow.genai.evaluate`` here: the runner depends only
on the abstract ``TurnDriver`` (satisfied by any transport ``Session``), the
``Scorer`` protocol, and the ``MetricsSink`` ABC.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from .aggregate import mean, percentile
from .case import EvalCase
from .judge import Judge
from .run_record import CompletionStatus, RunRecord
from .scorer import CaseResult, Score, Scorer, ScoringContext, TurnScope
from .sink import MetricsSink

logger = logging.getLogger("agent_evals.runner")


@runtime_checkable
class TurnDriver(Protocol):
    """Drives one conversation turn and returns a normalized ``RunRecord``.

    A transport ``Session`` (with auth/thread/timeout baked in) satisfies this.
    The runner asks the ``session_factory`` for a fresh driver per case so each
    scenario gets its own thread/history.
    """

    def ask(self, question: str) -> RunRecord:
        ...


class RunReport(BaseModel):
    run_name: str
    case_results: list[CaseResult] = Field(default_factory=list)
    aggregates: dict[str, Any] = Field(default_factory=dict)


# session_factory signature: (EvalCase) -> TurnDriver
from collections.abc import Callable  # noqa: E402


class Runner:
    def __init__(
        self,
        *,
        session_factory: "Callable[[EvalCase], TurnDriver]",
        scorers: list[Scorer],
        sink: MetricsSink,
        judge: Judge | None = None,
        config: dict | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.scorers = scorers
        self.sink = sink
        self.judge = judge
        self.config = config or {}

    # ------------------------------------------------------------------
    def run(self, cases: list[EvalCase], *, run_name: str, params: dict | None = None) -> RunReport:
        self.sink.start_run(name=run_name, params=params or {})
        case_results: list[CaseResult] = []
        all_runs: list[RunRecord] = []
        try:
            for case in cases:
                # Drive first, then decide applicability from what the agent's
                # tools actually returned THIS run — the eval must not depend on
                # frozen/seeded data (#31/#32). Operational metrics see every real
                # run, so a skipped case still counts toward latency/tokens.
                runs = self._drive_case(case)
                all_runs.extend(runs)
                facts = self._derive_facts(runs)
                derived_false, never_derived = self._classify_requires(case, facts)
                if derived_false:
                    # The fact-bearing tool ran and reported no data: a genuine
                    # environment condition, so the case does not apply here.
                    reason = f"precondition unmet in this environment: {derived_false}"
                    result = CaseResult(
                        case_id=case.id,
                        scores=[Score.skip("precondition", reason,
                                           requires=list(case.requires or []),
                                           unmet=derived_false, facts=facts)],
                        metadata={**dict(case.metadata),
                                  "precondition_skipped": True,
                                  "skip_reason": reason,
                                  "requires": list(case.requires or []),
                                  "derived_facts": facts},
                    )
                    if never_derived:
                        result.metadata["precondition_never_derived"] = never_derived
                else:
                    # never_derived alone is NOT a skip: the fact is absent because
                    # no tool produced it, which usually means the agent did not do
                    # the thing the case is about. Score it and flag it (E8).
                    result = self._score_case(case, runs)
                    result.metadata["derived_facts"] = facts
                    if never_derived:
                        result.metadata["precondition_never_derived"] = never_derived
                        result.metadata["requires"] = list(case.requires or [])
                self.sink.log_case_result(result, runs)
                case_results.append(result)
            aggregates = self._aggregate(case_results, all_runs)
            self.sink.log_summary(aggregates)
        finally:
            self.sink.end_run()
        return RunReport(run_name=run_name, case_results=case_results, aggregates=aggregates)

    # ------------------------------------------------------------------
    def _drive_case(self, case: EvalCase) -> list[RunRecord]:
        driver = self.session_factory(case)
        runs: list[RunRecord] = []
        for ti, turn in enumerate(case.as_turns()):
            rec = driver.ask(turn.question)
            rec.turn_index = ti
            runs.append(rec)
        return runs

    def _derive_facts(self, runs: list[RunRecord]) -> dict:
        """Read data-facts (e.g. ``has_matched_requisitions``) from THIS run's
        tool results, via the configured ``config['derive_facts']`` callable.
        Empty when none is wired (a pure capture → nothing is filtered)."""
        deriver = self.config.get("derive_facts")
        return deriver(runs) if callable(deriver) else {}

    def _classify_requires(self, case: EvalCase, facts: dict) -> tuple[list[str], list[str]]:
        """Split a case's unmet ``requires:`` into the two situations that look
        identical from the outside but mean opposite things (#31/#32, E8).

        Returns ``(derived_false, never_derived)``:

        - **derived_false**: the fact-bearing tool ran and reported no data, so
          the precondition genuinely does not hold in this environment. A skip.
        - **never_derived**: no tool produced the fact at all. Usually the agent
          did not perform the action the case is about, so this is a behavioural
          failure and must be scored, not skipped.

        Both empty means the case applies. Data-independent either way: the facts
        come from this environment's own run.
        """
        if not case.requires or not callable(self.config.get("derive_facts")):
            return [], []
        derived_false = [f for f in case.requires if f in facts and not facts[f]]
        never_derived = [f for f in case.requires if f not in facts]
        return derived_false, never_derived

    def _score_case(self, case: EvalCase, runs: list[RunRecord]) -> CaseResult:
        scores: list[Score] = []
        for scorer in self.scorers:
            spec = scorer.spec
            try:
                if spec.turn_scope == TurnScope.MULTI:
                    ctx = ScoringContext(
                        case=case, runs=runs, turn_index=len(runs) - 1,
                        judge=self.judge, config=self.config,
                    )
                    s = scorer.score(ctx)
                    s.details.setdefault("turn_index", "all")
                    scores.append(s)
                else:
                    for ti in range(len(runs)):
                        ctx = ScoringContext(
                            case=case, runs=runs, turn_index=ti,
                            judge=self.judge, config=self.config,
                        )
                        s = scorer.score(ctx)
                        s.details.setdefault("turn_index", ti)
                        scores.append(s)
            except Exception as exc:  # a scorer must never abort the run
                logger.exception("scorer %s failed on case %s", spec.metric, case.id)
                scores.append(Score.failed(spec.metric, f"{type(exc).__name__}: {exc}"))
        # Per-metric pass thresholds from calibration config (#28) override the
        # scorer's default, so the SME can tune pass/fail without code changes.
        thresholds = self.config.get("thresholds") or {}
        if thresholds:
            for s in scores:
                t = thresholds.get(s.metric)
                if t is not None and s.value is not None and not s.skipped and s.error is None:
                    s.with_threshold(float(t))
        return CaseResult(case_id=case.id, scores=scores, metadata=dict(case.metadata))

    def _aggregate(self, case_results: list[CaseResult], all_runs: list[RunRecord]) -> dict[str, Any]:
        from collections import defaultdict

        by_metric: dict[str, list[float]] = defaultdict(list)
        passes: dict[str, list[bool]] = defaultdict(list)
        errors: dict[str, int] = defaultdict(int)
        for cr in case_results:
            for s in cr.scores:
                if s.error is not None:
                    errors[s.metric] += 1
                    continue
                if s.skipped:
                    continue
                if s.value is not None:
                    by_metric[s.metric].append(s.value)
                if s.passed is not None:
                    passes[s.metric].append(s.passed)

        agg: dict[str, Any] = {}
        for metric, vals in by_metric.items():
            m = mean(vals)
            if m is not None:
                agg[f"{metric}.mean"] = m
                agg[f"{metric}.n"] = float(len(vals))
        for metric, flags in passes.items():
            if flags:
                agg[f"{metric}.pass_rate"] = sum(1 for f in flags if f) / len(flags)
        # visibility: a metric that errored (e.g. a failing judge) must never
        # just vanish from the summary — surface its error count.
        for metric, n in errors.items():
            agg[f"{metric}.errors"] = float(n)
        # case-level coverage + precondition skips, WITH per-case reasons in the
        # summary (reported, never silent).
        if case_results:
            skipped = [cr for cr in case_results if cr.metadata.get("precondition_skipped")]
            agg["cases.total"] = float(len(case_results))
            agg["cases.scored"] = float(len(case_results) - len(skipped))
            if skipped:
                agg["cases.skipped_precondition"] = float(len(skipped))
                agg["skipped_cases"] = [
                    {"case_id": cr.case_id,
                     "requires": cr.metadata.get("requires"),
                     "reason": cr.metadata.get("skip_reason")}
                    for cr in skipped
                ]

            # A precondition whose fact was never derived is scored, not skipped,
            # but it still needs its own bucket: it marks a case whose defining
            # action produced no evidence it happened (E8).
            undrivable = [cr for cr in case_results if cr.metadata.get("precondition_never_derived")]
            if undrivable:
                agg["cases.precondition_never_derived"] = float(len(undrivable))
                agg["precondition_never_derived_cases"] = [
                    {"case_id": cr.case_id,
                     "requires": cr.metadata.get("requires"),
                     "never_derived": cr.metadata.get("precondition_never_derived"),
                     "skipped": bool(cr.metadata.get("precondition_skipped"))}
                    for cr in undrivable
                ]

            # Run-wide fact evidence turns an ambiguous precondition into a
            # verdict: if one case derived a fact TRUE, the environment supports
            # it, so another case in the same run failing to derive it is the
            # agent's doing, not the environment's. This is the automatic form of
            # the "contradictory skip pair" tell (E8).
            evidence: dict[str, set[bool]] = defaultdict(set)
            for cr in case_results:
                for key, val in (cr.metadata.get("derived_facts") or {}).items():
                    evidence[key].add(bool(val))
            contradictions = []
            for cr in case_results:
                unmet = list(cr.metadata.get("precondition_never_derived") or [])
                if cr.metadata.get("precondition_skipped"):
                    unmet += [f for f in (cr.metadata.get("requires") or [])
                              if not (cr.metadata.get("derived_facts") or {}).get(f)]
                for fact in dict.fromkeys(unmet):
                    if True in evidence.get(fact, set()):
                        contradictions.append({
                            "case_id": cr.case_id,
                            "fact": fact,
                            "detail": (f"{fact!r} was derived true elsewhere in this run, so the "
                                       "environment supports it; this case did not produce it"),
                        })
            if contradictions:
                agg["precondition_contradictions"] = contradictions

        # A turn that reached an agent outside its declared envelope is a
        # containment breach, not a quality wobble, and must not be reachable
        # only by opening scores.jsonl. Averaged into a metric it can be
        # cancelled exactly by an unrelated improvement, which is how a
        # cross-persona misroute survived a whole run review (E18).
        route_violations = []
        for cr in case_results:
            for s in cr.scores:
                d = s.details or {}
                if not d.get("route_violation"):
                    continue
                routes = d.get("routes") or {}
                route_violations.append({
                    "case_id": cr.case_id,
                    "turn_index": d.get("turn_index"),
                    "expected_routes": routes.get("allowed"),
                    "observed_routes": routes.get("observed"),
                    "outside_envelope": routes.get("outside_envelope"),
                })
        if route_violations:
            agg["route_violations"] = route_violations
            agg["cases.route_violations"] = float(
                len({v["case_id"] for v in route_violations}))

        # operational: latency distribution across all runs
        ttfts = [r.timing.ttft_ms for r in all_runs]
        totals = [r.timing.total_ms for r in all_runs]
        for label, series in (("ttft_ms", ttfts), ("total_ms", totals)):
            for p in (50, 95, 99):
                v = percentile(series, p)
                if v is not None:
                    agg[f"latency.{label}.p{p}"] = v
        if all_runs:
            aborted = sum(1 for r in all_runs if r.completion_status != CompletionStatus.COMPLETED)
            agg["latency.abort_rate"] = aborted / len(all_runs)
            agg["runs.total"] = float(len(all_runs))

        # operational: token / cost across all runs (flagged when estimated)
        token_totals = [r.usage.total_tokens for r in all_runs if r.usage.total_tokens is not None]
        if token_totals:
            agg["tokens.total.sum"] = float(sum(token_totals))
            tmean = mean(token_totals)
            if tmean is not None:
                agg["tokens.total.mean"] = tmean
        costs = [r.usage.cost_usd for r in all_runs if r.usage.cost_usd is not None]
        if costs:
            agg["cost.usd.sum"] = float(sum(costs))
        if all_runs:
            estimated = sum(1 for r in all_runs if r.usage.source.value == "estimated")
            agg["tokens.estimated_fraction"] = estimated / len(all_runs)
        return agg
