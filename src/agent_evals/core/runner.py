"""The neutral orchestration spine.

load cases → drive a per-case session to get one RunRecord per turn → run the
applicable scorers → push scores + run artifacts to the sink → aggregate.

There is deliberately no ``mlflow.genai.evaluate`` here: the runner depends only
on the abstract ``TurnDriver`` (satisfied by any transport ``Session``), the
``Scorer`` protocol, and the ``MetricsSink`` ABC.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

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
    aggregates: dict[str, float] = Field(default_factory=dict)


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
                skip = self._precondition_skip(case)
                if skip is not None:
                    result = CaseResult(
                        case_id=case.id,
                        scores=[skip],
                        metadata={**dict(case.metadata),
                                  "precondition_skipped": True,
                                  "skip_reason": skip.skip_reason,
                                  "requires": list(case.requires or [])},
                    )
                    self.sink.log_case_result(result, [])
                    case_results.append(result)
                    continue
                runs = self._drive_case(case)
                all_runs.extend(runs)
                result = self._score_case(case, runs)
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

    def _precondition_skip(self, case: EvalCase) -> Score | None:
        """Skip a case whose profile-state precondition isn't met (#31).

        ``config['profile_state']`` is a dict of known facts (e.g.
        ``{'has_matched_requisitions': True}``). When it is absent (e.g. the
        transcript-capture run) nothing is filtered — every case runs. When it
        is present, a case ``requires:`` every listed fact to be truthy, else it
        is skipped with an explicit, reported reason (never silently)."""
        requires = case.requires
        if not requires:
            return None
        state = self.config.get("profile_state")
        if not state:
            return None
        unmet = [f for f in requires if not state.get(f)]
        if unmet:
            return Score.skip(
                "precondition",
                f"profile-state precondition unmet: {unmet}",
                requires=list(requires), unmet=unmet,
            )
        return None

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
        return CaseResult(case_id=case.id, scores=scores, metadata=dict(case.metadata))

    def _aggregate(self, case_results: list[CaseResult], all_runs: list[RunRecord]) -> dict[str, float]:
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

        agg: dict[str, float] = {}
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
        # case-level coverage + precondition skips (reported, never silent).
        if case_results:
            pc_skipped = sum(1 for cr in case_results if cr.metadata.get("precondition_skipped"))
            agg["cases.total"] = float(len(case_results))
            agg["cases.scored"] = float(len(case_results) - pc_skipped)
            if pc_skipped:
                agg["cases.skipped_precondition"] = float(pc_skipped)

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
