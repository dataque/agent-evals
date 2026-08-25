"""MLflow metrics sink — the FIRST metrics backend, and the ONLY module that
imports ``mlflow`` (lazily, inside methods). Maps our neutral ``Score`` →
MLflow metrics and our ``RunRecord``/``CaseResult`` → run artifacts (reusing
``JsonlSink`` for the artifact bundle, which keeps it byte-identical to the
portability-proof sink).

Porting to another framework = writing a sibling sink like this one; nothing in
``core``/``scorers``/``transport`` changes.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from ..core.run_record import RunRecord
from ..core.scorer import CaseResult
from ..core.sink import MetricsSink
from .jsonl_sink import JsonlSink

_BAD_KEY = re.compile(r"[^A-Za-z0-9_\-./ ]")


def _safe_key(key: str) -> str:
    return _BAD_KEY.sub("_", key)


def _flatten(d: dict, prefix: str = "") -> dict:
    out: dict = {}
    for k, v in (d or {}).items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, key + "."))
        else:
            out[key] = v if isinstance(v, (str, int, float, bool)) else str(v)
    return out


class MlflowSink(MetricsSink):
    def __init__(self, *, experiment: str = "agent-evals", tracking_uri: str | None = None,
                 artifact_path: str = "agent-evals") -> None:
        self.experiment = experiment
        self.tracking_uri = tracking_uri
        self.artifact_path = artifact_path
        self._case_idx = 0
        self._tmp: str | None = None
        self._jsonl: JsonlSink | None = None
        self._run_id: str | None = None

    def start_run(self, *, name: str, params: dict) -> None:
        import mlflow

        if self.tracking_uri:
            mlflow.set_tracking_uri(self.tracking_uri)
        mlflow.set_experiment(self.experiment)
        run = mlflow.start_run(run_name=name)
        # only needed to revise params after the run has ended; a backend that
        # does not hand one back simply gets no post-run refresh.
        self._run_id = getattr(getattr(run, "info", None), "run_id", None)
        flat = _flatten(params)
        if flat:
            mlflow.log_params({_safe_key(k): v for k, v in flat.items()})
        self._case_idx = 0
        self._tmp = tempfile.mkdtemp(prefix="agent-evals-")
        self._jsonl = JsonlSink(out_dir=self._tmp)
        self._jsonl.start_run(name="run", params=params)

    def update_params(self, params: dict) -> None:
        """Record a post-run parameter revision (E19: the backend's model is not
        readable until its meter exists).

        An MLflow param is write-once, so re-logging ``backend.model`` with a
        different value would be rejected. The revision therefore lands as *tags*
        (mutable) and in the artifact bundle's ``params.json``, which is the copy
        a reader of the run bundle actually opens. Best-effort: a provenance
        refresh must never fail a run that has already completed.
        """
        import mlflow

        if self._jsonl is not None:
            self._jsonl.update_params(params)
        if not self._run_id:
            return
        try:
            client = mlflow.tracking.MlflowClient()
            for key, value in _flatten({"backend": params.get("backend") or {}}).items():
                client.set_tag(self._run_id, _safe_key(key), value)
            if self._tmp is not None:
                client.log_artifact(self._run_id, str(Path(self._tmp) / "run" / "params.json"),
                                    artifact_path=self.artifact_path)
        except Exception as exc:  # noqa: BLE001 - provenance is not worth a crash
            print(f"NOTE: could not update MLflow params after the run: {exc}")

    def log_case_result(self, result: CaseResult, runs: list[RunRecord]) -> None:
        import mlflow

        for s in result.scores:
            if s.value is not None and not s.skipped and s.error is None:
                mlflow.log_metric(_safe_key(s.metric), float(s.value), step=self._case_idx)
        assert self._jsonl is not None
        self._jsonl.log_case_result(result, runs)
        self._case_idx += 1

    def log_summary(self, aggregates: dict) -> None:
        import mlflow

        numeric = {_safe_key(k): float(v) for k, v in aggregates.items() if isinstance(v, (int, float))}
        if numeric:
            mlflow.log_metrics(numeric)
        assert self._jsonl is not None
        self._jsonl.log_summary(aggregates)

    def end_run(self) -> None:
        import mlflow

        try:
            if self._jsonl is not None:
                self._jsonl.end_run()
            if self._tmp is not None:
                mlflow.log_artifacts(str(Path(self._tmp) / "run"), artifact_path=self.artifact_path)
        finally:
            mlflow.end_run()
