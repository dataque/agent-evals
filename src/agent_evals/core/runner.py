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
                unmet = self._unmet_requires(case, facts)
                if unmet is not None:
                    reason = f"precondition unmet in this environment: {unmet}"
                    result = CaseResult(
                        case_id=case.id,
                        scores=[Score.skip("precondition", reason,
                                           requires=list(case.requires or []), unmet=unmet, facts=facts)],
                        metadata={**dict(case.metadata),
                                  "precondition_skipped": True,
                                  "skip_reason": reason,
                                  "requires": list(case.requires or []),
                                  "derived_facts": facts},
                    )
                else:
                    result = self._score_case(case, runs)
                    result.metadata["derived_facts"] = facts
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

    def _unmet_requires(self, case: EvalCase, facts: dict) -> list[str] | None:
        """Which of a case's ``requires:`` preconditions are NOT satisfied by the
        run-derived facts (#31/#32). ``None`` when the case has no preconditions,
        or no deriver is configured (capture mode → run everything). A non-empty
        list means the case is skipped with an explicit, reported reason — never
        silently. Data‑independent: the facts come from this env's own run."""
        if not case.requires or not callable(self.config.get("derive_facts")):
            return None
        unmet = [f for f in case.requires if not facts.get(f)]
        return unmet or None

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
