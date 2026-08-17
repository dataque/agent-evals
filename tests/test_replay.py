"""Replay round-trip + the guard rails that keep it honest.

The whole point of replay is that a metric which moves did so because a scorer
changed, not because the agent or the dataset did. That only holds while the
recording still answers the dataset's questions, so the guard rails below are
load-bearing, not defensive extras.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from agent_evals.core.case import EvalCase
from agent_evals.core.run_record import DerivedTiming, RunRecord, StreamHealth
from agent_evals.core.runner import Runner
from agent_evals.core.scorer import Family, Score, ScorerSpec, TurnScope
from agent_evals.replay import (
    ReplayError,
    build_replay_factory,
    load_recorded_runs,
    reconcile,
)
from agent_evals.sinks import JsonlSink


class _EchoLen:
    """Deterministic and input-sensitive, so a wrong transcript shows up."""

    spec = ScorerSpec(metric="string_check", number=22, title="t",
                      family=Family.DETERMINISTIC, turn_scope=TurnScope.SINGLE)

    def score(self, ctx):
        return Score(metric=self.spec.metric, value=float(len(ctx.run.assistant_text)))


class _LiveDriver:
    def __init__(self, case):
        self.case = case
        self.n = 0

    def ask(self, question: str) -> RunRecord:
        self.n += 1
        return RunRecord(
            thread_id=f"th-{self.case.id}", run_id=f"{self.case.id}-{self.n}",
            user_id="A", user_message=question, assistant_text=f"answer to {question}",
            timing=DerivedTiming(ttft_ms=1.0, total_ms=2.0),
            stream_health=StreamHealth(run_started_seen=True, run_finished_seen=True),
        )


def _cases() -> list[EvalCase]:
    return [
        EvalCase.from_raw({"inputs": {"question": "one"}}, id="single"),
        EvalCase.from_raw(
            {"inputs": {"scenario": "s", "turns": [{"question": "a"}, {"question": "b"}]}},
            id="multi",
        ),
    ]


def _capture(tmp: Path) -> Path:
    """Produce a real jsonl run dir the way the CLI would."""
    Runner(session_factory=_LiveDriver, scorers=[_EchoLen()],
           sink=JsonlSink(out_dir=str(tmp))).run(_cases(), run_name="src", params={})
    return tmp / "src"


def test_replay_reproduces_the_live_scores_exactly():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        src = _capture(tmp)
        by_case = load_recorded_runs(src)
        assert set(by_case) == {"single", "multi"}
        assert [len(v) for v in by_case.values()] == [1, 2]

        Runner(session_factory=build_replay_factory(by_case), scorers=[_EchoLen()],
               sink=JsonlSink(out_dir=str(tmp))).run(
            _cases(), run_name="replayed", params={"replay": True})

        def scores(name):
            return [
                (r["case_id"], r["details"].get("turn_index"), r["value"])
                for r in map(json.loads, (tmp / name / "scores.jsonl").read_text().splitlines())
            ]

        assert scores("replayed") == scores("src")


def test_runs_jsonl_is_self_attributing():
    """case_id on every run row, so replay never depends on file ordering."""
    with tempfile.TemporaryDirectory() as td:
        src = _capture(Path(td))
        rows = [json.loads(x) for x in (src / "runs.jsonl").read_text().splitlines()]
        assert [r["case_id"] for r in rows] == ["single", "multi", "multi"]


def test_grouping_falls_back_to_write_order_for_legacy_runs():
    """Runs captured before case_id was added must still be replayable."""
    with tempfile.TemporaryDirectory() as td:
        src = _capture(Path(td))
        rows = [json.loads(x) for x in (src / "runs.jsonl").read_text().splitlines()]
        for r in rows:
            r.pop("case_id")
        (src / "runs.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")

        by_case = load_recorded_runs(src)
        assert list(by_case) == ["single", "multi"]
        assert [len(v) for v in by_case.values()] == [1, 2]


def test_edited_question_aborts_rather_than_scoring_a_stale_transcript():
    with tempfile.TemporaryDirectory() as td:
        by_case = load_recorded_runs(_capture(Path(td)))
        edited = [EvalCase.from_raw({"inputs": {"question": "one, revised"}}, id="single")]
        factory = build_replay_factory(by_case)
        with pytest.raises(ReplayError, match="edited since this run"):
            for case in edited:
                driver = factory(case)
                for turn in case.as_turns():
                    driver.ask(turn.question)


def test_turn_count_change_aborts():
    with tempfile.TemporaryDirectory() as td:
        by_case = load_recorded_runs(_capture(Path(td)))
        grown = EvalCase.from_raw(
            {"inputs": {"scenario": "s", "turns": [{"question": "a"}, {"question": "b"},
                                                    {"question": "c"}]}},
            id="multi",
        )
        with pytest.raises(ReplayError, match="3 turn\\(s\\) but 2 were recorded"):
            build_replay_factory(by_case)(grown)


def test_unrecorded_case_aborts():
    with tempfile.TemporaryDirectory() as td:
        by_case = load_recorded_runs(_capture(Path(td)))
        fresh = EvalCase.from_raw({"inputs": {"question": "brand new"}}, id="added-later")
        with pytest.raises(ReplayError, match="not present in the recording"):
            build_replay_factory(by_case)(fresh)


def test_reconcile_reports_drift_in_both_directions():
    with tempfile.TemporaryDirectory() as td:
        by_case = load_recorded_runs(_capture(Path(td)))
        cases = [
            EvalCase.from_raw({"inputs": {"question": "one"}}, id="single"),
            EvalCase.from_raw({"inputs": {"question": "new"}}, id="added-later"),
        ]
        replayable, missing_rec, missing_suite = reconcile(cases, by_case)
        assert [c.id for c in replayable] == ["single"]
        assert missing_rec == ["added-later"]
        assert missing_suite == ["multi"]
