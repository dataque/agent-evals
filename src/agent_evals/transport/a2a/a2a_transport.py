"""Optional A2A transport — proves the Transport seam is real.

Speaks JSON-RPC ``message/send`` and normalizes the (non-streaming) response
envelope into the SAME ``RunRecord`` the SSE adapter produces. A2A supplies a
server-side ``execution_trace`` artifact (tool calls/routes) and reported token
usage; it cannot supply client-observed latency / stream-health (those are
SSE-only). Scorers are unchanged — that is the point.
"""

from __future__ import annotations

import time
import uuid

import httpx

from ...core.run_record import (
    CompletionStatus,
    DerivedTiming,
    NormalizedMessage,
    RunError,
    RunRecord,
    SubagentRoute,
    TokenUsage,
    ToolCall,
    ToolStatus,
    UsageSource,
)
from ..base import SessionState, TurnRequest


def _extract_text(result: dict) -> str:
    parts: list[str] = []
    msg = (result.get("status") or {}).get("message") or {}
    for p in msg.get("parts", []) or []:
        if p.get("kind") == "text" and p.get("text"):
            parts.append(p["text"])
    if not parts:
        for art in result.get("artifacts", []) or []:
            for p in art.get("parts", []) or []:
                if p.get("kind") == "text" and p.get("text"):
                    parts.append(p["text"])
    return "".join(parts)


def _extract_trace(result: dict) -> dict:
    for art in result.get("artifacts", []) or []:
        if art.get("name") == "execution_trace":
            for p in art.get("parts", []) or []:
                if p.get("kind") == "data" and isinstance(p.get("data"), dict):
                    return p["data"]
    return {}


def _tool_calls_from_trace(trace: dict) -> list[ToolCall]:
    calls: list[ToolCall] = []
    pending: dict[str, list[ToolCall]] = {}
    for ev in trace.get("events", []) or []:
        kind = ev.get("type")
        data = ev.get("data") or {}
        if kind == "tool_call":
            tc = ToolCall(
                tool_call_id=str(ev.get("span_id") or data.get("tool_name") or f"tc{len(calls)}"),
                name=data.get("tool_name"),
                args=data.get("args") or {},
                status=ToolStatus.MISSING_RESULT,
                owning_subagent=ev.get("agent_id"),
            )
            calls.append(tc)
            pending.setdefault(tc.name or "", []).append(tc)
        elif kind == "tool_result":
            name = data.get("tool_name") or ""
            queue = pending.get(name) or []
            tc = queue.pop(0) if queue else None
            if tc is not None:
                tc.result = data.get("result", data.get("output"))
                status = str(data.get("status", "")).lower()
                tc.status = ToolStatus.OK if status in ("", "ok", "success", "completed") else ToolStatus.ERROR
                tc.is_error = tc.status == ToolStatus.ERROR
    return calls


def _routes_from_trace(trace: dict) -> list[SubagentRoute]:
    routes: list[SubagentRoute] = []
    for ev in trace.get("events", []) or []:
        if ev.get("type") == "route":
            target = (ev.get("data") or {}).get("route_to")
            if target:
                routes.append(SubagentRoute(subagent=target, via="route_event"))
    return routes


def _usage_from_metadata(md: dict, ns: str) -> TokenUsage:
    tokens = md.get(f"{ns}.tokens") or {}
    cost = md.get(f"{ns}.cost") or {}
    if not tokens and not cost:
        return TokenUsage(source=UsageSource.UNKNOWN)
    it, ot = tokens.get("input"), tokens.get("output")
    tt = tokens.get("total")
    if tt is None and (it is not None or ot is not None):
        tt = (it or 0) + (ot or 0)
    by_subagent = md.get(f"{ns}.agent_token_totals")
    return TokenUsage(
        source=UsageSource.REPORTED, input_tokens=it, output_tokens=ot, total_tokens=tt,
        cost_usd=cost.get("usd"), cost_rates_version=cost.get("rates_version"),
        by_subagent=by_subagent if isinstance(by_subagent, dict) else None,
    )


class A2ATransport:
    def __init__(self, url: str, *, metadata_ns: str = "hr-agent", headers: dict | None = None,
                 http_transport: "httpx.BaseTransport | None" = None,
                 verify: "bool | str" = True) -> None:
        self.url = url
        self.metadata_ns = metadata_ns
        self.headers = headers or {}
        self.http_transport = http_transport
        self.verify = verify

    def run_turn(self, turn: TurnRequest, session: SessionState) -> RunRecord:
        mid = str(uuid.uuid4())
        payload = {
            "jsonrpc": "2.0",
            "id": mid,
            "method": "message/send",
            "params": {
                "message": {
                    "kind": "message",
                    "messageId": mid,
                    "role": "user",
                    "contextId": session.thread_id,
                    "parts": [{"kind": "text", "text": turn.user_message}],
                }
            },
        }
        token = turn.identity.token_provider.get_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", **self.headers}

        t0 = time.perf_counter()
        data: dict | None = None
        transport_error: str | None = None
        client_kwargs: dict = {"timeout": turn.timeout_s}
        if self.http_transport is not None:
            client_kwargs["transport"] = self.http_transport
        else:
            client_kwargs["verify"] = self.verify
        try:
            with httpx.Client(**client_kwargs) as client:
                resp = client.post(self.url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            transport_error = f"{type(exc).__name__}: {exc}"
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        rec = self._to_record(turn, session, mid, data, transport_error, elapsed_ms)
        session.turn_index += 1
        return rec

    def _to_record(self, turn, session, mid, data, transport_error, elapsed_ms) -> RunRecord:
        common = dict(thread_id=session.thread_id, turn_index=session.turn_index,
                      user_id=turn.identity.user_id, transport="a2a", user_message=turn.user_message)
        if data is None:
            return RunRecord(run_id=mid, completion_status=CompletionStatus.ERRORED,
                             error=RunError(message=transport_error or "no response"),
                             timing=DerivedTiming(total_ms=elapsed_ms, aborted=True), **common)

        result = data.get("result") or {}
        text = _extract_text(result)
        trace = _extract_trace(result)
        metadata = result.get("metadata") or {}
        state = (result.get("status") or {}).get("state")

        status = CompletionStatus.COMPLETED
        error = None
        md_err = metadata.get(f"{self.metadata_ns}.error")
        if data.get("error"):
            status, error = CompletionStatus.ERRORED, RunError(message=str(data["error"]))
        elif md_err:
            status, error = CompletionStatus.ERRORED, RunError(message=str(md_err))
        elif state in ("failed", "error", "canceled", "rejected"):
            status, error = CompletionStatus.ERRORED, RunError(message=f"task state={state}")

        lat = metadata.get(f"{self.metadata_ns}.latency_ms")
        timing = DerivedTiming(
            total_ms=float(lat) if isinstance(lat, (int, float)) else elapsed_ms,
            aborted=status != CompletionStatus.COMPLETED,
        )
        return RunRecord(
            run_id=str(result.get("id") or mid),
            assistant_text=text,
            messages=[NormalizedMessage(id=f"{mid}-a", role="assistant", content=text)] if text else [],
            tool_calls=_tool_calls_from_trace(trace),
            subagent_routes=_routes_from_trace(trace),
            usage=_usage_from_metadata(metadata, self.metadata_ns),
            timing=timing,
            completion_status=status,
            error=error,
            final_state={"state": state} if state else None,
            **common,
        )
