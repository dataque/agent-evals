"""The metrics-backend seam.

A ``MetricsSink`` receives per-case scores, run artifacts, and the run summary.
MLflow is one implementation; a local JSONL writer is another. Porting the eval
system to a different framework means writing one new ``MetricsSink`` — nothing
in ``core``/``scorers``/``transport`` changes. This is why ``import mlflow`` is
confined to ``agent_evals.sinks.mlflow_sink``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .run_record import RunRecord
from .scorer import CaseResult


class MetricsSink(ABC):
    @abstractmethod
    def start_run(self, *, name: str, params: dict) -> None:
        """Open a logical evaluation run and record its parameters."""

    @abstractmethod
    def log_case_result(self, result: CaseResult, runs: list[RunRecord]) -> None:
        """Persist one case's scores plus its per-turn run artifacts."""

    @abstractmethod
    def log_summary(self, aggregates: dict) -> None:
        """Record the aggregate metrics for the whole run."""

    @abstractmethod
    def end_run(self) -> None:
        """Close the run and flush."""

    # Optional sugar; runner calls start_run/end_run explicitly.
    def __enter__(self) -> "MetricsSink":
        return self

    def __exit__(self, *exc: object) -> bool:
        self.end_run()
        return False
