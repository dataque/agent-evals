"""#25 Follow-up Pills Correctness — the turn emits the expected follow-up pill
set.

Backend UX contract (post pills-refactor): a server-side ``NextStepsAdvisor``
classifies each completed orchestrator turn (deterministic scenario cascade,
else an LLM scenario judge) and emits at most ONE AG-UI CUSTOM event
``{name: "NEXT_STEPS", value: [{id, suggestion}]}``. The scenario id is NOT on
the wire — cases may keep ``expected_scenario_id`` as derivational metadata
(which scenario the pills were derived from), but scoring is the pill-set match
on ``expected_pills``; the scorer skips when ``expected_pills`` is absent.

Legacy captures (the retired ``emit_followups`` tool result / ``{pills,
scenarioId}`` blobs) are still extracted so old runs.jsonl re-score.
"""

from __future__ import annotations

from ..core.aggregate import precision_recall_f1
from ..core.run_record import RunRecord
from ..core.scorer import Family, Score, ScorerSpec, ScoringContext, TurnScope

NEXT_STEPS_EVENT_NAME = "NEXT_STEPS"


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
    """Legacy shape: ``{pills: [...], scenarioId | scenario_id: ...}``."""
    if isinstance(blob, dict) and ("pills" in blob or "scenarioId" in blob or "scenario_id" in blob):
        sid = blob.get("scenarioId") or blob.get("scenario_id")
        return (str(sid) if sid else None), _pill_texts(blob.get("pills"))
    return None


def _next_steps_pills(payload: object) -> list[str] | None:
    """Pill texts from a NEXT_STEPS CUSTOM event payload
    (``{name: "NEXT_STEPS", value: [{id, suggestion}]}``), else ``None``."""
    if not isinstance(payload, dict):
        return None
    name = payload.get("name")
    if not (isinstance(name, str) and name.upper() == NEXT_STEPS_EVENT_NAME):
        return None
    out: list[str] = []
    value = payload.get("value")
    if isinstance(value, list):
        for v in value:
            if isinstance(v, str):
                t = v.strip()
            elif isinstance(v, dict):
                t = str(v.get("suggestion") or v.get("text") or "").strip()
            else:
                t = ""
            if t:
                out.append(t)
    return out


def extract_pills(run: RunRecord) -> tuple[str | None, list[str], int]:
    """Find the emitted pills. Returns ``(scenario_id, pill_texts, n_next_steps_events)``.

    Primary path: NEXT_STEPS CUSTOM events (``scenario_id`` is always ``None``
    there — it does not ride the wire; the last emission wins and the count
    flags double-emits). Legacy fallbacks (``emit_followups`` tool result, then
    any ``{pills, scenarioId}`` blob in a tool result or event) keep old
    captures scoreable."""
    emissions: list[list[str]] = []
    for ev in run.events:
        pills = _next_steps_pills(ev.payload)
        if pills is not None:
            emissions.append(pills)
    if emissions:
        return None, emissions[-1], len(emissions)

    # legacy: emit_followups (preferred), then any tool result with the pills shape
    for tc in run.tool_calls:
        if (tc.name or "").lower() == "emit_followups":
            if (hit := _from_blob(tc.result)) is not None:
                return hit[0], hit[1], 0
    for tc in run.tool_calls:
        if (hit := _from_blob(tc.result)) is not None:
            return hit[0], hit[1], 0
    # legacy server-side resolver: a CUSTOM/RAW event payload (possibly nested)
    for ev in run.events:
        blob = ev.payload or {}
        if (hit := _from_blob(blob)) is not None:
            return hit[0], hit[1], 0
        if isinstance(blob, dict):
            for key in ("value", "data", "custom", "snapshot"):
                if (hit := _from_blob(blob.get(key))) is not None:
                    return hit[0], hit[1], 0
    return None, [], 0


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
        exp_pills = ctx.expectations.expected_pills
        exp_sid = ctx.expectations.expected_scenario_id
        if exp_pills is None:
            reason = (
                "no expected_pills"
                if exp_sid is None
                else "expected_scenario_id only — the scenario id is not on the NEXT_STEPS wire; author expected_pills"
            )
            return Score.skip(self.spec.metric, reason)

        obs_sid, obs_pills, n_events = extract_pills(ctx.run)
        _, _, fv = precision_recall_f1(exp_pills, obs_pills)
        details: dict = {
            "observed_pills": obs_pills,
            "expected_pills": list(exp_pills),
            "pills_f1": fv,
            "pills_missing": sorted(set(exp_pills) - set(obs_pills)),
            "pills_unexpected": sorted(set(obs_pills) - set(exp_pills)),
            "next_steps_event_count": n_events,
        }
        if n_events > 1:
            details["double_emit"] = True
        if exp_sid is not None:
            # derivational metadata only — kept for traceability, never scored
            details["expected_scenario_id"] = exp_sid
            details["observed_scenario_id"] = obs_sid
        return Score(
            metric=self.spec.metric,
            value=fv,
            rationale=f"pills={obs_pills} (next_steps_events={n_events})",
            details=details,
        ).with_threshold(1.0)
