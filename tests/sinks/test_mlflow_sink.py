"""Framework-independence proof (offline): the MlflowSink logs our Scores to
MLflow AND writes an artifact bundle byte-identical to the JsonlSink, using a
recording fake ``mlflow`` module (so it runs without the heavy dependency)."""

from __future__ import annotations

import sys
import tempfile
import types
from pathlib import Path

from agent_evals.core.run_record import DerivedTiming, RunRecord, StreamHealth, ToolCall, ToolStatus
from agent_evals.core.runner import Runner
from agent_evals.core.scorer import Family, Score, ScorerSpec, TurnScope
from agent_evals.sinks import JsonlSink, MlflowSink


def _fake_mlflow(record: dict) -> types.ModuleType:
    m = types.ModuleType("mlflow")
    m.set_tracking_uri = lambda u: record.__setitem__("tracking_uri", u)
    m.set_experiment = lambda e: record.__setitem__("experiment", e)
    m.start_run = lambda run_name=None: record.__setitem__("run_name", run_name)
    m.log_params = lambda p: record.setdefault("params", {}).update(p)
    m.log_metric = lambda k, v, step=None: record.setdefault("metrics", []).append((k, v, step))
    m.log_metrics = lambda d: record.setdefault("summary", {}).update(d)
    m.log_artifacts = lambda d, artifact_path=None: record.update(artifacts_dir=d, artifact_path=artifact_path)
    m.end_run = lambda: record.__setitem__("ended", True)
    return m


class _Tooled:
    spec = ScorerSpec(metric="tool_selection_accuracy", number=2, title="t",
                      family=Family.DETERMINISTIC, turn_scope=TurnScope.SINGLE)

    def score(self, ctx):
        return Score(metric=self.spec.metric, value=1.0).with_threshold(1.0)


class _FixedDriver:
    def __init__(self, case):
        self.case = case

    def ask(self, question: str) -> RunRecord:
        return RunRecord(
            thread_id="t", run_id="fixed", user_id="A", user_message=question,
            assistant_text="ok", tool_calls=[ToolCall(tool_call_id="c", name="suggest_skills",
                                                       result={"top": [], "additional": []}, status=ToolStatus.OK)],
            timing=DerivedTiming(ttft_ms=10.0, total_ms=20.0),
            stream_health=StreamHealth(run_started_seen=True, run_finished_seen=True),
        )


def _run_into(sink, cases):
    Runner(session_factory=_FixedDriver, scorers=[_Tooled()], sink=sink).run(
        cases, run_name="r", params={"k": "v"})


def test_mlflow_sink_parity_and_logging(monkeypatch):
    from agent_evals.core.case import EvalCase

    cases = [EvalCase.from_raw({"inputs": {"question": "Suggest skills"}}, id="c1")]

    record: dict = {}
    monkeypatch.setitem(sys.modules, "mlflow", _fake_mlflow(record))

    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        _run_into(JsonlSink(out_dir=a), cases)        # reference artifacts
        _run_into(MlflowSink(experiment="exp"), cases)  # logs to fake mlflow + its own jsonl

        # MLflow received the right calls
        assert record["experiment"] == "exp"
        assert record["params"]["k"] == "v"
        assert ("tool_selection_accuracy", 1.0, 0) in record["metrics"]
        assert record["summary"]["tool_selection_accuracy.mean"] == 1.0
        assert record["ended"] is True

        # the artifact bundle MLflow logged is byte-identical to the JsonlSink's
        ref = Path(a) / "r"
        mlf = Path(record["artifacts_dir"])  # == <tmp>/run
        for name in ("cases.jsonl", "runs.jsonl", "scores.jsonl", "summary.json"):
            assert (mlf / name).read_text() == (ref / name).read_text(), name
