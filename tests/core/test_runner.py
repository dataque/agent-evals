"""Core spine: aggregation math, case parsing, and a full fake-driver run."""

from __future__ import annotations

import tempfile

from agent_evals.core import (
    EvalCase,
    Family,
    RunRecord,
    Runner,
    Score,
    ScorerSpec,
    ToolCall,
    ToolStatus,
    TurnScope,
    f1,
    percentile,
)
from agent_evals.sinks import JsonlSink


def test_f1_semantics():
    assert f1({"a", "b"}, {"a", "b"}) == 1.0
    assert f1(set(), set()) == 1.0          # both empty -> perfect
    assert f1({"a"}, set()) == 0.0          # one empty -> zero
    assert abs(f1({"a", "b"}, {"a"}) - (2 * 1.0 * 0.5 / 1.5)) < 1e-9


def test_percentile_interpolates():
    assert percentile([10, 20, 30, 40], 50) == 25.0
    assert percentile([], 95) is None
    assert percentile([7], 99) == 7.0


def test_case_parsing_single_and_multi():
    single = EvalCase.from_raw(
        {"inputs": {"question": "Q"}, "expectations": {"response_must_contain": ["x"]}},
        id="c1",
    )
    multi = EvalCase.from_raw(
        {"inputs": {"scenario": "s", "turns": [{"question": "a"}, {"question": "b"}]}},
        id="c2",
    )
    assert not single.is_multi_turn and len(single.as_turns()) == 1
    assert multi.is_multi_turn and len(multi.as_turns()) == 2
    assert single.expectations.response_must_contain == ["x"]


class _CalledATool:
    spec = ScorerSpec(
        metric="called_a_tool", number=0, title="Called a tool",
        family=Family.DETERMINISTIC, turn_scope=TurnScope.SINGLE,
    )

    def score(self, ctx):
        n = len(ctx.run.tool_calls)
        return Score(metric=self.spec.metric, value=1.0 if n else 0.0).with_threshold(1.0)


class _FakeDriver:
    def __init__(self, case):
        self.case = case
        self.i = 0

    def ask(self, question: str) -> RunRecord:
        self.i += 1
        return RunRecord(
            thread_id="t1", run_id=f"r{self.i}", user_id="TEST0001",
            user_message=question, assistant_text=f"answer: {question}",
            tool_calls=[ToolCall(tool_call_id="c1", name="suggest_skills",
                                 args={"x": 1}, result={"top": []}, status=ToolStatus.OK)],
        )


def test_runner_full_round_trip():
    cases = [
        EvalCase.from_raw({"inputs": {"question": "Suggest skills"}}, id="c1"),
        EvalCase.from_raw(
            {"inputs": {"scenario": "s", "turns": [{"question": "hi"}, {"question": "bye"}]}},
            id="c2",
        ),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        sink = JsonlSink(out_dir=tmp)
        runner = Runner(session_factory=_FakeDriver, scorers=[_CalledATool()], sink=sink)
        report = runner.run(cases, run_name="smoke", params={"k": "v"})

    assert report.aggregates["called_a_tool.mean"] == 1.0
    assert report.aggregates["called_a_tool.pass_rate"] == 1.0
    assert report.aggregates["runs.total"] == 3.0  # 1 + 2 turns
    assert report.aggregates["latency.abort_rate"] == 0.0
    # 3 score rows total (case-1: 1 turn, case-2: 2 turns)
    total_scores = sum(len(cr.scores) for cr in report.case_results)
    assert total_scores == 3
