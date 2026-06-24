"""#25 Follow-up Pills Correctness — the turn emits the expected follow-up
``scenario_id`` and the exact pill set.

Backend-specific UX contract (an addition beyond the original 24 metrics): the
agent attaches up to 3 follow-up "pills" per turn via ``emit_followups`` (the LLM
path) or a server-side resolver. Skipped unless the case declares
``expected_scenario_id`` and/or ``expected_pills``, so it is inert for agents
that have no such contract.
"""

from __future__ import annotations

from ..core.aggregate import precision_recall_f1
from ..core.run_record import RunRecord
from ..core.scorer import Family, Score, ScorerSpec, ScoringContext, TurnScope


def _pill_texts(pills: object) -> list[str]:
    out: list[str] = []
    if isinstance(pills, list):
        for p in pills:
            if isinstance(p, str):
                t = p.strip()
            elif isinstance(p, dict):
                t = str(p.get("text") or p.get("label") or p.get("title") or "").strip()
            else:
                t = ""
            if t:
                out.append(t)
    return out


def _from_blob(blob: object) -> tuple[str | None, list[str]] | None:
    if isinstance(blob, dict) and ("pills" in blob or "scenarioId" in blob or "scenario_id" in blob):
        sid = blob.get("scenarioId") or blob.get("scenario_id")
        return (str(sid) if sid else None), _pill_texts(blob.get("pills"))
    return None


def extract_pills(run: RunRecord) -> tuple[str | None, list[str]]:
    """Find the emitted ``(scenario_id, pill_texts)``. Handles the LLM path
    (``emit_followups`` tool result) and a server-side resolver carrying the same
    ``{pills, scenarioId}`` shape in a tool result or a CUSTOM event payload. The
    capture run confirms which path the backend actually uses."""
    # 1) emit_followups (preferred), then any tool result with the pills shape
    for tc in run.tool_calls:
        if (tc.name or "").lower() == "emit_followups":
            if (hit := _from_blob(tc.result)) is not None:
                return hit
    for tc in run.tool_calls:
        if (hit := _from_blob(tc.result)) is not None:
            return hit
    # 2) server-side resolver: a CUSTOM/RAW event payload (possibly nested)
    for ev in run.events:
        blob = ev.payload or {}
        if (hit := _from_blob(blob)) is not None:
            return hit
        if isinstance(blob, dict):
            for key in ("value", "data", "custom", "snapshot"):
                if (hit := _from_blob(blob.get(key))) is not None:
                    return hit
    return None, []


class FollowupPillsCorrectness:
    spec = ScorerSpec(
        metric="followup_pills_correctness",
        number=25,
        title="Follow-up Pills Correctness",
        family=Family.DETERMINISTIC,
        turn_scope=TurnScope.SINGLE,
        needs_golden=True,
        requires_fields=["tool_calls", "events"],
    )

    def score(self, ctx: ScoringContext) -> Score:
        exp_sid = ctx.expectations.expected_scenario_id
        exp_pills = ctx.expectations.expected_pills
        if exp_sid is None and exp_pills is None:
            return Score.skip(self.spec.metric, "no expected_scenario_id / expected_pills")

        obs_sid, obs_pills = extract_pills(ctx.run)
        parts: list[float] = []
        details: dict = {"observed_scenario_id": obs_sid, "observed_pills": obs_pills}

        if exp_sid is not None:
            scenario_ok = 1.0 if obs_sid == exp_sid else 0.0
            parts.append(scenario_ok)
            details["expected_scenario_id"] = exp_sid
            details["scenario_ok"] = bool(scenario_ok)
        if exp_pills is not None:
            _, _, fv = precision_recall_f1(exp_pills, obs_pills)
            parts.append(fv)
            details["expected_pills"] = list(exp_pills)
            details["pills_f1"] = fv
            details["pills_missing"] = sorted(set(exp_pills) - set(obs_pills))
            details["pills_unexpected"] = sorted(set(obs_pills) - set(exp_pills))

        value = sum(parts) / len(parts) if parts else None
        return Score(
            metric=self.spec.metric,
            value=value,
            rationale=f"scenario={obs_sid!r} pills={obs_pills}",
            details=details,
        ).with_threshold(1.0)
