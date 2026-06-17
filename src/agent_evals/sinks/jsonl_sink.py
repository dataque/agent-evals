"""A dependency-free metrics sink that writes runs/scores/summary to disk.

This is the portability proof: the runner produces identical ``Score`` objects
whether they land here or in MLflow. ``MlflowSink`` mirrors this exact contract.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TextIO

from ..core.run_record import RunRecord
from ..core.scorer import CaseResult
from ..core.sink import MetricsSink


def _dumps(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


class JsonlSink(MetricsSink):
    """Writes ``<out_dir>/<run_name>/{params.json, cases.jsonl, runs.jsonl,
    scores.jsonl, summary.json}``."""

    def __init__(self, out_dir: str = "eval-runs") -> None:
        self.out_dir = Path(out_dir)
        self._run_dir: Path | None = None
        self._cases_f: TextIO | None = None
        self._runs_f: TextIO | None = None
        self._scores_f: TextIO | None = None

    def start_run(self, *, name: str, params: dict) -> None:
        self._run_dir = self.out_dir / name
        self._run_dir.mkdir(parents=True, exist_ok=True)
        (self._run_dir / "params.json").write_text(json.dumps(params, indent=2, default=str))
        self._cases_f = open(self._run_dir / "cases.jsonl", "w", encoding="utf-8")
        self._runs_f = open(self._run_dir / "runs.jsonl", "w", encoding="utf-8")
        self._scores_f = open(self._run_dir / "scores.jsonl", "w", encoding="utf-8")

    def log_case_result(self, result: CaseResult, runs: list[RunRecord]) -> None:
        assert self._cases_f and self._runs_f and self._scores_f, "start_run() not called"
        self._cases_f.write(_dumps(result.model_dump(mode="json")) + "\n")
        self._cases_f.flush()
        for score in result.scores:
            row = {"case_id": result.case_id, **score.model_dump(mode="json")}
            self._scores_f.write(_dumps(row) + "\n")
        self._scores_f.flush()
        for rec in runs:
            self._runs_f.write(_dumps(rec.model_dump(mode="json")) + "\n")
        self._runs_f.flush()

    def log_summary(self, aggregates: dict) -> None:
        assert self._run_dir is not None, "start_run() not called"
        (self._run_dir / "summary.json").write_text(json.dumps(aggregates, indent=2, default=str))

    def end_run(self) -> None:
        for f in (self._cases_f, self._runs_f, self._scores_f):
            if f is not None:
                try:
                    f.close()
                except Exception:
                    pass
        self._cases_f = self._runs_f = self._scores_f = None
