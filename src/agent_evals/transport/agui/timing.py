"""Derive client-observed timing primitives from the event log (metric #13)."""

from __future__ import annotations

from ...core.run_record import DerivedTiming, Event, ToolCall
from .events import CONTENT_BEARING, ET


def derive_timing(events: list[Event], tool_calls: list[ToolCall], *, aborted: bool) -> DerivedTiming:
    per_first: dict[str, float] = {}
    per_count: dict[str, int] = {}
    for e in events:
        per_count[e.type] = per_count.get(e.type, 0) + 1
        if e.type not in per_first and e.arrival_ms is not None:
            per_first[e.type] = e.arrival_ms

    ttft: float | None = None
    for e in events:
        if e.type in CONTENT_BEARING and e.arrival_ms is not None:
            ttft = e.arrival_ms
            break

    total = per_first.get(ET.RUN_FINISHED)
    if total is None:
        arrivals = [e.arrival_ms for e in events if e.arrival_ms is not None]
        total = max(arrivals) if arrivals else None

    tool_latencies: dict[str, float] = {}
    for tc in tool_calls:
        if tc.started_arrival_ms is not None and tc.result_arrival_ms is not None:
            tool_latencies[tc.tool_call_id] = tc.result_arrival_ms - tc.started_arrival_ms

    return DerivedTiming(
        ttft_ms=ttft,
        total_ms=total,
        request_to_run_started_ms=per_first.get(ET.RUN_STARTED),
        per_event_type_first_ms=per_first,
        per_event_type_count=per_count,
        tool_latencies_ms=tool_latencies,
        aborted=aborted,
    )
