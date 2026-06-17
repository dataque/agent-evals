"""Project a transport-neutral ``RunRecord`` into the row/trace shape that
backend-specific tools expect (e.g. MLflow's ``mlflow.genai.evaluate`` and the
prior harness's trace-aware scorers).

Keeping this OUT of ``RunRecord`` is deliberate: the record stays neutral, and
each metrics backend gets its own projection. Honors the empty-trace→None rule
(``mlflow``'s ``Trace.from_dict`` raises on an empty dict).
"""

from __future__ import annotations

from ..core.run_record import RunRecord, ToolStatus


def runrecord_to_trace(rec: RunRecord) -> dict | None:
    """Build a ``{"events": [...]}`` trace with ``tool_call``/``tool_result``/
    ``route`` events, matching the shape the legacy trace scorers read.

    Returns ``None`` (not ``{}``) when there is nothing to project.
    """
    events: list[dict] = []
    for route in rec.subagent_routes:
        events.append({"type": "route", "data": {"route_to": route.subagent}})
    for tc in rec.tool_calls:
        events.append({"type": "tool_call", "data": {"tool_name": tc.name, "args": tc.args or {}}})
        status = "ok" if tc.status == ToolStatus.OK else tc.status.value
        events.append({"type": "tool_result", "data": {"tool_name": tc.name, "status": status}})
    if not events:
        return None
    return {"events": events}


def to_mlflow_row(rec: RunRecord, expectations: dict | None = None) -> dict:
    """Project a run into the column shape ``mlflow.genai.evaluate`` consumes."""
    return {
        "inputs": {"question": rec.user_message},
        "outputs": rec.assistant_text,
        "expectations": expectations or {},
        "trace": runrecord_to_trace(rec),
        "state": rec.final_state or {},
    }
