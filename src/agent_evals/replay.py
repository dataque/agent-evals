"""Re-score a frozen run: serve recorded ``RunRecord``s instead of calling the agent.

Every scorer or calibration change moves a number. Without replay, telling "the
scorer changed" apart from "the agent changed" needs a fresh live run, and a live
run carries agent non-determinism and judge variance on top of the change under
test. Replay removes both: the transcripts are fixed, so a deterministic metric
that moves did so because of the code change and nothing else.

This works because ``TurnDriver`` is a one-method protocol (``ask``), so a driver
that reads from disk substitutes for a transport ``Session`` with no runner
change at all.

**Replay is only valid while the dataset still matches the recording.** Two guard
rails enforce that, and both fail loudly rather than scoring a new question
against an old transcript:

1. turn counts must match, per case, checked before any scoring;
2. each replayed question must equal the recorded ``user_message``.

Once cases are re-authored, their recordings are dead and the affected cases must
be re-captured live. That is expected, and the assertions are what make it
obvious instead of silent.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from .core.case import EvalCase
from .core.run_record import RunRecord


class ReplayError(RuntimeError):
    """The recording does not match the dataset being replayed against."""


def load_recorded_runs(run_dir: str | Path) -> dict[str, list[RunRecord]]:
    """Map ``case_id -> [RunRecord, ...]`` (turn order) from a jsonl run dir.

    Runs written before ``case_id`` was added to ``runs.jsonl`` carry no case
    attribution, so fall back to the sink's write order: ``log_case_result`` is
    called once per case, in dataset order, and writes that case's records
    contiguously. A record with ``turn_index == 0`` therefore starts a new case,
    and the resulting groups line up 1:1 with ``cases.jsonl``.
    """
    run_dir = Path(run_dir)
    runs_path, cases_path = run_dir / "runs.jsonl", run_dir / "cases.jsonl"
    if not runs_path.exists():
        raise ReplayError(f"no runs.jsonl in {run_dir}: not a jsonl run directory")

    rows = [json.loads(line) for line in runs_path.read_text().splitlines() if line.strip()]
    if not rows:
        raise ReplayError(f"{runs_path} is empty")

    if all(row.get("case_id") for row in rows):
        by_case: dict[str, list[RunRecord]] = {}
        for row in rows:
            by_case.setdefault(row.pop("case_id"), []).append(RunRecord(**row))
        return by_case

    groups: list[list[RunRecord]] = []
    for row in rows:
        row.pop("case_id", None)
        rec = RunRecord(**row)
        if rec.turn_index == 0 or not groups:
            groups.append([])
        groups[-1].append(rec)

    if not cases_path.exists():
        raise ReplayError(
            f"{runs_path} has no case_id and there is no cases.jsonl to attribute it with"
        )
    case_ids = [
        json.loads(line)["case_id"]
        for line in cases_path.read_text().splitlines()
        if line.strip()
    ]
    if len(case_ids) != len(groups):
        raise ReplayError(
            f"cannot attribute records in {run_dir}: cases.jsonl has {len(case_ids)} cases "
            f"but runs.jsonl groups into {len(groups)}. The run's artifacts are inconsistent."
        )
    return dict(zip(case_ids, groups))


class ReplayDriver:
    """Serves pre-recorded ``RunRecord``s for one case, in turn order."""

    def __init__(self, case_id: str, records: list[RunRecord]) -> None:
        self._case_id = case_id
        self._records = records
        self._i = 0

    def ask(self, question: str) -> RunRecord:
        if self._i >= len(self._records):
            raise ReplayError(
                f"{self._case_id}: asked {self._i + 1} turns but only "
                f"{len(self._records)} were recorded"
            )
        rec = self._records[self._i]
        self._i += 1
        if rec.user_message.strip() != question.strip():
            raise ReplayError(
                f"{self._case_id} turn {self._i - 1}: the case has been edited since this run.\n"
                f"  dataset: {question!r}\n"
                f"  recorded: {rec.user_message!r}\n"
                "Re-capture this case live; its recording no longer answers it."
            )
        return rec


def build_replay_factory(
    records_by_case: dict[str, list[RunRecord]],
) -> Callable[[EvalCase], ReplayDriver]:
    """A ``session_factory`` the runner can drive, backed by recorded records."""

    def factory(case: EvalCase) -> ReplayDriver:
        records = records_by_case.get(case.id)
        if records is None:
            raise ReplayError(f"{case.id}: not present in the recording")
        n_turns = len(case.as_turns())
        if n_turns != len(records):
            raise ReplayError(
                f"{case.id}: the case has {n_turns} turn(s) but {len(records)} were recorded. "
                "The case has been edited since this run; re-capture it live."
            )
        return ReplayDriver(case.id, records)

    return factory


def reconcile(
    cases: list[EvalCase], records_by_case: dict[str, list[RunRecord]]
) -> tuple[list[EvalCase], list[str], list[str]]:
    """Split the suite against a recording.

    Returns ``(replayable, missing_from_recording, missing_from_suite)``.
    ``replayable`` keeps dataset order so the re-scored artifacts line up with a
    live run's.
    """
    recorded = set(records_by_case)
    replayable = [c for c in cases if c.id in recorded]
    missing_from_recording = [c.id for c in cases if c.id not in recorded]
    suite_ids = {c.id for c in cases}
    missing_from_suite = [cid for cid in records_by_case if cid not in suite_ids]
    return replayable, missing_from_recording, missing_from_suite
