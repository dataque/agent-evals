"""End-to-end integration: run ALL registered scorers over the bundled HR suite
with a fake transport + the HeuristicJudge, through the real Runner + JsonlSink.
Proves every scorer composes and nothing throws on realistic records."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from agent_evals.core.run_record import (
    DerivedTiming,
    RunRecord,
    StreamHealth,
    SubagentRoute,
    TokenUsage,
    ToolCall,
    ToolStatus,
    UsageSource,
)
from agent_evals.core.runner import Runner
from agent_evals.datasets import load_suite
from agent_evals.judges import HeuristicJudge, apply_per_metric_judges
from agent_evals.scorers import build_registry, get_scorers
from agent_evals.sinks import JsonlSink

import pytest

import agent_evals.datasets as _ds

# The bundled HR suite (datasets/hr/*.yaml) is gitignored / not shipped; skip this
# suite-dependent integration test when it isn't present locally (e.g. a fresh clone).
_HR_SUITE = Path(_ds.__file__).parent / "hr"
pytestmark = pytest.mark.skipif(
    not (_HR_SUITE.is_dir() and any(_HR_SUITE.glob("*.y*ml"))),
    reason="bundled hr suite not present (gitignored) — provide a suite to run this",
)

_SCHEMA_RESULTS = {
    "suggest_skills": {"top": [{"name": "Python", "source": "AI_INFERRED"}], "additional": []},
    "suggest_requisitions": {"matches": [{"requisition": {"requisitionId": "R1"}, "matchScore": "HIGH"}]},
    "analyze_talent_profile": {"talentProfile": {}, "missingSections": [], "nextActions": []},
    "draft_message": {"recipients": [{"email": "a@b.com"}], "subject": "Hello", "body": "..."},
}


class _FakeDriver:
    """Returns canned RunRecords that satisfy each turn's expected tools."""

    def __init__(self, case):
        self.case = case
        self.turn = 0

    def ask(self, question: str) -> RunRecord:
        exp = self.case.as_turns()[self.turn].expectations
        self.turn += 1
        names = list(exp.expected_tool_calls or []) + list(exp.expected_actions or [])
        args_map = exp.expected_tool_args or {}
        tools = [
            ToolCall(tool_call_id=n, name=n, args=dict(args_map.get(n, {})),
                     result=_SCHEMA_RESULTS.get(n, {"ok": True}), status=ToolStatus.OK)
            for n in dict.fromkeys(names)
        ]
        routes = [SubagentRoute(subagent=r, via="step_name") for r in (exp.expected_routes or [])]
        text = ("Here are your skills: Java, React."
                if "what did i just add" in question.lower()
                else f"Response addressing: {question}")
        return RunRecord(
            thread_id="t", run_id=f"r{self.turn}", user_id="TEST0001", user_message=question,
            assistant_text=text, tool_calls=tools, subagent_routes=routes,
            timing=DerivedTiming(ttft_ms=100.0 + self.turn, total_ms=1000.0 + self.turn),
            stream_health=StreamHealth(run_started_seen=True, run_finished_seen=True),
            usage=TokenUsage(source=UsageSource.ESTIMATED, input_tokens=10, output_tokens=20, total_tokens=30),
        )


def test_full_run_over_hr_suite_no_scorer_errors():
    cases = load_suite("hr")
    assert len(cases) >= 6

    scorers = get_scorers("all")
    assert len(scorers) == len(build_registry()) == 25  # 24 in-scope + follow-up pills (#25)
    apply_per_metric_judges(scorers, default=HeuristicJudge())

    with tempfile.TemporaryDirectory() as tmp:
        sink = JsonlSink(out_dir=tmp)
        runner = Runner(session_factory=_FakeDriver, scorers=scorers, sink=sink,
                        judge=HeuristicJudge(), config={"rubric": "Is the response professional?"})
        report = runner.run(cases, run_name="it", params={"suite": "hr"})

        # no scorer raised (errors are captured, not thrown)
        errored = [s for cr in report.case_results for s in cr.scores if s.error]
        assert not errored, errored

        agg = report.aggregates
        assert agg["stream_health.mean"] == 1.0
        assert agg["tool_selection_accuracy.mean"] == 1.0          # fake satisfies expected tools
        assert agg.get("tool_argument_correctness.mean", 1.0) == 1.0  # current clusters gate args to {} → skipped; real args land post-capture
        assert "latency.ttft_ms.p50" in agg and "tokens.total.sum" in agg
        assert agg["tokens.estimated_fraction"] == 1.0
        # judged + golden-driven metrics that were previously skipped now run
        assert "topic_adherence.mean" in agg          # judge on non-empty text
        assert "user_feedback_signal.mean" in agg     # metadata.user_feedback present (#23)

        run_dir = Path(tmp) / "it"
        for f in ("cases.jsonl", "runs.jsonl", "scores.jsonl", "summary.json", "params.json"):
            assert (run_dir / f).exists()
        # every scored metric appears in scores.jsonl
        metrics_logged = {json.loads(line)["metric"]
                          for line in (run_dir / "scores.jsonl").read_text().splitlines()}
        assert "knowledge_retention" in metrics_logged  # multi-turn scorer fired
        assert "plan_quality" in metrics_logged
