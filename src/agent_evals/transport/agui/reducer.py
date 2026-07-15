"""Pure reduction of an AG-UI event stream into ``RunRecord`` parts.

This is the heart of the transport layer and is deliberately free of any I/O so
it can be unit-tested against canned event lists. The SSE transport is a thin
shell that feeds parsed :class:`Event` objects in here.

Key behaviors (all verified against the backend wire contract):
- assistant text is assembled from TEXT_MESSAGE_* (and CHUNK) by messageId;
- tool calls pair START/ARGS(accumulate delta)/END with a later RESULT by
  toolCallId; args are the concatenated arg fragments parsed as JSON;
- subagent routing is *synthesized* from STEP names and ``Task`` tool calls
  (the SSE stream has no explicit route event);
- final state = STATE_SNAPSHOT then applied STATE_DELTA (RFC-6902 JSON-Patch);
- protocol-invariant breaches are recorded in ``StreamHealth`` (metric #24),
  never raised.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field

from ...core.run_record import (
    CompletionStatus,
    Event,
    NormalizedMessage,
    ReasoningSegment,
    RunError,
    Step,
    StreamHealth,
    SubagentRoute,
    ToolCall,
    ToolStatus,
)
from .events import ET, KNOWN_EVENT_TYPES

# Keys a `Task` router tool might use to name its target subagent.
_SUBAGENT_ARG_KEYS = (
    "subagent", "subagent_type", "subagentType", "agent", "agent_name", "agentName", "subAgent",
)


@dataclass
class ReducedRun:
    assistant_text: str = ""
    messages: list[NormalizedMessage] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    reasoning: list[ReasoningSegment] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    subagent_routes: list[SubagentRoute] = field(default_factory=list)
    final_state: dict | None = None
    stream_health: StreamHealth = field(default_factory=StreamHealth)
    error: RunError | None = None
    completion_status: CompletionStatus = CompletionStatus.COMPLETED


class _ToolBuilder:
    def __init__(self, tool_call_id: str):
        self.id = tool_call_id
        self.name: str | None = None
        self.parent_message_id: str | None = None
        self.args_parts: list[str] = []
        self.started_ms: float | None = None
        self.ended_ms: float | None = None
        self.result_raw: str | None = None
        self.result: object | None = None
        self.result_ms: float | None = None
        self.result_message_id: str | None = None
        self.result_role: str | None = None
        self.has_start = False
        self.has_end = False
        self.has_result = False
        self.owning_step_index: int | None = None
        self.owning_subagent: str | None = None
        self.args_parse_error: str | None = None


# Markers for a tool result returned as a bare error STRING (e.g. a backend
# deserialization/stack-trace message) rather than a structured {error: ...} body.
# Without these, a failed mutating call looks OK and audit/action (#16) false-passes.
_STRING_ERROR_MARKERS = (
    "cannot construct", "cannot deserialize", "deserialize", "not of type",
    "exception", "stack trace", "traceback", "could not", "failed to", "rejected",
    "nothing was saved",
)


def _is_error_result(result: object) -> bool:
    if isinstance(result, dict):
        if result.get("error"):
            return True
        status = result.get("status")
        if isinstance(status, str) and status.lower() in ("error", "failed", "failure"):
            return True
        # ToolResponse envelope {status, data:{result: "<ack text>"}}: a SUCCESS
        # envelope can still carry a no-op/failure message (e.g. save_skills
        # "... Nothing was saved."); scan the ack text so #16 doesn't false-pass.
        data = result.get("data")
        if isinstance(data, dict) and isinstance(data.get("result"), str):
            low = data["result"].lower()
            return any(m in low for m in _STRING_ERROR_MARKERS)
        return False
    if isinstance(result, str):
        low = result.lower()
        return any(m in low for m in _STRING_ERROR_MARKERS)
    return False


def reduce_events(events: list[Event], *, aborted_timeout: bool = False) -> ReducedRun:
    health = StreamHealth()
    out = ReducedRun(stream_health=health)

    # text message buffers: msgid -> {role, parts, started_ms, open}
    text_buf: dict[str, dict] = {}
    text_order: list[str] = []

    tools: dict[str, _ToolBuilder] = {}
    tool_order: list[str] = []

    open_step_stack: list[int] = []
    current_subagent: str | None = None

    # reasoning buffers
    reasoning_open: dict | None = None

    run_started_count = 0
    messages_snapshot: list[dict] | None = None

    def current_step_index() -> int | None:
        return open_step_stack[-1] if open_step_stack else None

    def finalize_text(msgid: str) -> None:
        buf = text_buf.get(msgid)
        if not buf or buf.get("done"):
            return
        buf["done"] = True
        content = "".join(buf["parts"])
        role = buf.get("role") or "assistant"
        out.messages.append(NormalizedMessage(id=msgid, role=role, content=content))
        if role in ("assistant", "reasoning"):
            out._assistant_parts = getattr(out, "_assistant_parts", [])
            out._assistant_parts.append(content)

    for e in events:
        et = e.type
        p = e.payload
        if et not in KNOWN_EVENT_TYPES and et not in health.unknown_event_types:
            health.unknown_event_types.append(et)

        if et == ET.RUN_STARTED:
            run_started_count += 1
            health.run_started_seen = True

        elif et == ET.RUN_FINISHED:
            health.run_finished_seen = True

        elif et == ET.RUN_ERROR:
            out.error = RunError(
                message=str(p.get("message", "run error")),
                code=p.get("code"),
                arrival_ms=e.arrival_ms,
            )

        # ---- text messages ----
        elif et == ET.TEXT_MESSAGE_START:
            mid = p.get("messageId") or p.get("id") or f"msg-{e.seq}"
            text_buf[mid] = {"role": p.get("role", "assistant"), "parts": [], "started_ms": e.arrival_ms}
            text_order.append(mid)
        elif et == ET.TEXT_MESSAGE_CONTENT:
            mid = p.get("messageId") or p.get("id")
            if mid not in text_buf:
                health.ordering_violations.append(f"TEXT_MESSAGE_CONTENT before START ({mid})")
                text_buf[mid] = {"role": "assistant", "parts": [], "started_ms": e.arrival_ms}
                text_order.append(mid)
            text_buf[mid]["parts"].append(str(p.get("delta", "")))
        elif et == ET.TEXT_MESSAGE_CHUNK:
            mid = p.get("messageId") or p.get("id") or "chunk"
            if mid not in text_buf:
                text_buf[mid] = {"role": p.get("role", "assistant"), "parts": [], "started_ms": e.arrival_ms}
                text_order.append(mid)
            text_buf[mid]["parts"].append(str(p.get("delta", "")))
        elif et == ET.TEXT_MESSAGE_END:
            mid = p.get("messageId") or p.get("id")
            if mid not in text_buf:
                health.ordering_violations.append(f"TEXT_MESSAGE_END without START ({mid})")
            else:
                finalize_text(mid)

        # ---- tool calls ----
        elif et == ET.TOOL_CALL_START:
            tid = p.get("toolCallId") or p.get("id") or f"tool-{e.seq}"
            b = tools.get(tid) or _ToolBuilder(tid)
            b.has_start = True
            b.name = p.get("toolCallName") or p.get("toolName") or b.name
            b.parent_message_id = p.get("parentMessageId")
            b.started_ms = e.arrival_ms
            b.owning_step_index = current_step_index()
            b.owning_subagent = current_subagent
            if tid not in tools:
                tools[tid] = b
                tool_order.append(tid)
        elif et == ET.TOOL_CALL_ARGS:
            tid = p.get("toolCallId") or p.get("id")
            b = tools.get(tid)
            if b is None:
                health.ordering_violations.append(f"TOOL_CALL_ARGS before START ({tid})")
                b = _ToolBuilder(tid)
                tools[tid] = b
                tool_order.append(tid)
            b.args_parts.append(str(p.get("delta", "")))
        elif et == ET.TOOL_CALL_CHUNK:
            tid = p.get("toolCallId") or p.get("id") or f"tool-{e.seq}"
            b = tools.get(tid) or _ToolBuilder(tid)
            if tid not in tools:
                tools[tid] = b
                tool_order.append(tid)
            b.has_start = True
            if p.get("toolCallName"):
                b.name = p.get("toolCallName")
            if b.started_ms is None:
                b.started_ms = e.arrival_ms
                b.owning_step_index = current_step_index()
                b.owning_subagent = current_subagent
            if p.get("delta"):
                b.args_parts.append(str(p.get("delta", "")))
        elif et == ET.TOOL_CALL_END:
            tid = p.get("toolCallId") or p.get("id")
            b = tools.get(tid)
            if b is None:
                health.unmatched_tool_ends.append(str(tid))
            else:
                b.has_end = True
                b.ended_ms = e.arrival_ms
                raw_args = "".join(b.args_parts)
                if raw_args.strip():
                    try:
                        b._parsed_args = json.loads(raw_args)
                    except Exception as exc:
                        b.args_parse_error = str(exc)
                        b._parsed_args = None
                        health.malformed_arg_tool_calls.append(str(tid))
                else:
                    b._parsed_args = {}
                # Synthesize routing from a Task router tool call.
                if (b.name or "").lower() == "task":
                    sub = _extract_subagent(getattr(b, "_parsed_args", None))
                    if sub:
                        out.subagent_routes.append(
                            SubagentRoute(subagent=sub, via="task_tool", arrival_ms=e.arrival_ms)
                        )
                        current_subagent = sub
        elif et == ET.TOOL_CALL_RESULT:
            tid = p.get("toolCallId") or p.get("id")
            b = tools.get(tid)
            if b is None:
                health.orphan_tool_results.append(str(tid))
                b = _ToolBuilder(tid)
                tools[tid] = b
                tool_order.append(tid)
            b.has_result = True
            b.result_raw = p.get("content")
            b.result_ms = e.arrival_ms
            b.result_message_id = p.get("messageId")
            b.result_role = p.get("role")
            if isinstance(b.result_raw, str):
                try:
                    b.result = json.loads(b.result_raw)
                except Exception:
                    b.result = b.result_raw
            else:
                b.result = b.result_raw
            out.messages.append(
                NormalizedMessage(
                    id=b.result_message_id or f"toolmsg-{e.seq}",
                    role="tool",
                    content=b.result_raw if isinstance(b.result_raw, str) else json.dumps(b.result_raw),
                    tool_call_id=tid,
                )
            )

        # ---- steps / routing ----
        elif et == ET.STEP_STARTED:
            name = p.get("stepName") or p.get("name")
            idx = len(out.steps)
            out.steps.append(Step(name=name, started_arrival_ms=e.arrival_ms))
            open_step_stack.append(idx)
            if name:
                out.subagent_routes.append(
                    SubagentRoute(subagent=name, via="step_name", arrival_ms=e.arrival_ms)
                )
                current_subagent = name
        elif et == ET.STEP_FINISHED:
            if open_step_stack:
                idx = open_step_stack.pop()
                out.steps[idx].ended_arrival_ms = e.arrival_ms
            else:
                health.ordering_violations.append("STEP_FINISHED without STEP_STARTED")

        # ---- reasoning / thinking ----
        elif et in (ET.REASONING_MESSAGE_START, ET.THINKING_TEXT_MESSAGE_START):
            kind = "reasoning" if et == ET.REASONING_MESSAGE_START else "thinking"
            reasoning_open = {"kind": kind, "parts": [], "started_ms": e.arrival_ms}
        elif et in (ET.REASONING_MESSAGE_CONTENT, ET.THINKING_TEXT_MESSAGE_CONTENT):
            if reasoning_open is None:
                kind = "reasoning" if et == ET.REASONING_MESSAGE_CONTENT else "thinking"
                reasoning_open = {"kind": kind, "parts": [], "started_ms": e.arrival_ms}
            reasoning_open["parts"].append(str(p.get("delta", "")))
        elif et in (ET.REASONING_MESSAGE_END, ET.THINKING_TEXT_MESSAGE_END):
            if reasoning_open is not None:
                out.reasoning.append(
                    ReasoningSegment(
                        kind=reasoning_open["kind"],
                        text="".join(reasoning_open["parts"]),
                        started_arrival_ms=reasoning_open.get("started_ms"),
                        ended_arrival_ms=e.arrival_ms,
                    )
                )
                reasoning_open = None

        # ---- state ----
        elif et == ET.STATE_SNAPSHOT:
            snap = p.get("snapshot")
            out.final_state = copy.deepcopy(snap) if isinstance(snap, (dict, list)) else snap
        elif et == ET.STATE_DELTA:
            delta = p.get("delta")
            if out.final_state is None and not isinstance(delta, list):
                health.ordering_violations.append("STATE_DELTA before STATE_SNAPSHOT")
            base = out.final_state if out.final_state is not None else {}
            try:
                import jsonpatch

                out.final_state = jsonpatch.apply_patch(base, delta or [], in_place=False)
            except Exception as exc:
                health.state_patch_errors.append(str(exc))

        elif et == ET.MESSAGES_SNAPSHOT:
            msgs = p.get("messages")
            if isinstance(msgs, list):
                messages_snapshot = msgs

    # ---- finalize open buffers ----
    for mid in text_order:
        if not text_buf[mid].get("done"):
            finalize_text(mid)
        # an open text buffer that never got END is recorded but not fatal
    if reasoning_open is not None:
        out.reasoning.append(
            ReasoningSegment(
                kind=reasoning_open["kind"],
                text="".join(reasoning_open["parts"]),
                started_arrival_ms=reasoning_open.get("started_ms"),
            )
        )

    out.assistant_text = "".join(getattr(out, "_assistant_parts", []))

    # ---- build ToolCall list + tool health ----
    def sort_key(tid: str):
        ms = tools[tid].started_ms
        return (ms is None, ms if ms is not None else 0.0)

    for tid in sorted(tool_order, key=sort_key):
        b = tools[tid]
        status = _tool_status(b, health)
        out.tool_calls.append(
            ToolCall(
                tool_call_id=tid,
                name=b.name,
                args=getattr(b, "_parsed_args", None),
                args_raw="".join(b.args_parts) or None,
                args_parse_error=b.args_parse_error,
                result=b.result,
                result_raw=b.result_raw if isinstance(b.result_raw, str) else None,
                status=status,
                is_error=_is_error_result(b.result),
                parent_message_id=b.parent_message_id,
                result_message_id=b.result_message_id,
                result_role=b.result_role,
                started_arrival_ms=b.started_ms,
                ended_arrival_ms=b.ended_ms,
                result_arrival_ms=b.result_ms,
                owning_step_index=b.owning_step_index,
                owning_subagent=b.owning_subagent,
            )
        )

    # ---- prefer an authoritative messages snapshot when present ----
    if messages_snapshot is not None:
        out.messages = [
            NormalizedMessage(
                id=str(m.get("id", f"snap-{i}")),
                role=str(m.get("role", "assistant")),
                content=m.get("content"),
                name=m.get("name"),
                tool_calls=m.get("toolCalls"),
                tool_call_id=m.get("toolCallId"),
            )
            for i, m in enumerate(messages_snapshot)
        ]

    # ---- lifecycle / completion ----
    health.duplicate_run_started = run_started_count > 1
    health.ended_before_finished = not health.run_finished_seen
    if aborted_timeout:
        out.completion_status = CompletionStatus.ABORTED_TIMEOUT
    elif out.error is not None:
        out.completion_status = CompletionStatus.ERRORED
    elif health.run_finished_seen:
        out.completion_status = CompletionStatus.COMPLETED
    else:
        out.completion_status = CompletionStatus.TRUNCATED

    return out


def _tool_status(b: _ToolBuilder, health: StreamHealth) -> ToolStatus:
    if not b.has_start and b.has_result:
        return ToolStatus.ORPHAN_RESULT
    if b.has_start and not b.has_end:
        health.unmatched_tool_starts.append(b.id)
        return ToolStatus.INCOMPLETE
    if b.args_parse_error:
        return ToolStatus.BAD_ARGS
    if b.has_end and not b.has_result:
        health.tool_calls_missing_result.append(b.id)
        return ToolStatus.MISSING_RESULT
    if _is_error_result(b.result):
        return ToolStatus.ERROR
    return ToolStatus.OK


def _extract_subagent(args: object) -> str | None:
    if not isinstance(args, dict):
        return None
    for key in _SUBAGENT_ARG_KEYS:
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None
