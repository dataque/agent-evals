"""A2A transport normalizes a JSON-RPC envelope into the SAME RunRecord shape,
including a reported token usage that the SSE transport can only estimate."""

from __future__ import annotations

import json

import httpx

from agent_evals.core.run_record import CompletionStatus, ToolStatus, UsageSource
from agent_evals.transport import A2ATransport, Identity, LocalJwtMinter, Session

_RESULT = {
    "jsonrpc": "2.0", "id": "x",
    "result": {
        "id": "task1", "contextId": "ctx",
        "status": {"state": "completed",
                   "message": {"parts": [{"kind": "text", "text": "Here are your matches."}]}},
        "artifacts": [{
            "name": "execution_trace",
            "parts": [{"kind": "data", "data": {"events": [
                {"type": "route", "data": {"route_to": "requisition-matching-agent"}},
                {"type": "tool_call", "span_id": "s1",
                 "data": {"tool_name": "suggest_requisitions", "args": {"limit": 5}}},
                {"type": "tool_result",
                 "data": {"tool_name": "suggest_requisitions", "status": "ok", "result": {"matches": []}}},
            ]}}],
        }],
        "metadata": {
            "hr-agent.tokens": {"input": 100, "output": 50, "total": 150},
            "hr-agent.cost": {"usd": 0.003, "rates_version": "2024-12"},
            "hr-agent.latency_ms": 2500,
        },
    },
}


def _session(handler) -> Session:
    t = A2ATransport("http://backend/a2a", http_transport=httpx.MockTransport(handler))
    return Session(t, Identity(user_id="A", token_provider=LocalJwtMinter("A")))


def test_a2a_normalizes_to_runrecord():
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        assert body["method"] == "message/send"
        assert body["params"]["message"]["parts"][0]["text"]
        return httpx.Response(200, json=_RESULT)

    rec = _session(handler).ask("Find me roles")
    assert rec.transport == "a2a"
    assert rec.completion_status == CompletionStatus.COMPLETED
    assert rec.assistant_text == "Here are your matches."
    assert rec.tool_names() == ["suggest_requisitions"]
    tc = rec.tool_calls[0]
    assert tc.args == {"limit": 5} and tc.status == ToolStatus.OK and tc.result == {"matches": []}
    assert {r.subagent for r in rec.subagent_routes} == {"requisition-matching-agent"}
    # A2A reports real usage (vs. SSE which estimates)
    assert rec.usage.source == UsageSource.REPORTED
    assert rec.usage.total_tokens == 150 and rec.usage.cost_usd == 0.003
    assert rec.timing.total_ms == 2500


def test_a2a_failed_task_is_errored():
    failed = {"jsonrpc": "2.0", "id": "x", "result": {"id": "t", "status": {"state": "failed"}}}
    rec = _session(lambda req: httpx.Response(200, json=failed)).ask("hi")
    assert rec.completion_status == CompletionStatus.ERRORED and rec.error is not None


def test_a2a_http_error_is_errored():
    rec = _session(lambda req: httpx.Response(500, json={"error": "boom"})).ask("hi")
    assert rec.completion_status == CompletionStatus.ERRORED
