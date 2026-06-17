"""End-to-end transport path offline: AgUiSseTransport against a mock backend.

Proves the full chain — build RunAgentInput → POST → parse SSE (incl. the
leading-space wire quirk) → reduce → timing → usage → RunRecord — plus
multi-turn session accumulation and the auth-failure path.
"""

from __future__ import annotations

import json

import httpx

from agent_evals.core.run_record import CompletionStatus, UsageSource
from agent_evals.transport import AgUiSseTransport, Identity, LocalJwtMinter, Session


def _sse_wire(events: list[dict]) -> bytes:
    # Reproduce the backend quirk: each data value is prefixed with a space, so
    # after SSE strips one leading space a residual one remains for lstrip().
    buf = ""
    for e in events:
        buf += "data:  " + json.dumps(e) + "\n\n"
    return buf.encode("utf-8")


TURN1 = [
    {"type": "RUN_STARTED", "timestamp": 1},
    {"type": "TEXT_MESSAGE_START", "messageId": "m1", "role": "assistant", "timestamp": 2},
    {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "Here are your skills.", "timestamp": 3},
    {"type": "TEXT_MESSAGE_END", "messageId": "m1", "timestamp": 4},
    {"type": "TOOL_CALL_START", "toolCallId": "tc1", "toolCallName": "suggest_skills", "timestamp": 5},
    {"type": "TOOL_CALL_ARGS", "toolCallId": "tc1", "delta": "{}", "timestamp": 6},
    {"type": "TOOL_CALL_END", "toolCallId": "tc1", "timestamp": 7},
    {"type": "TOOL_CALL_RESULT", "toolCallId": "tc1", "content": '{"top":[],"additional":[]}', "timestamp": 8},
    {"type": "RUN_FINISHED", "timestamp": 9},
]
TURN2 = [
    {"type": "RUN_STARTED", "timestamp": 1},
    {"type": "TEXT_MESSAGE_START", "messageId": "m2", "role": "assistant", "timestamp": 2},
    {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m2", "delta": "Saved.", "timestamp": 3},
    {"type": "TEXT_MESSAGE_END", "messageId": "m2", "timestamp": 4},
    {"type": "RUN_FINISHED", "timestamp": 5},
]


def _make_transport(handler) -> AgUiSseTransport:
    return AgUiSseTransport("http://backend/api/v1/bff/ai/agent/sse",
                            http_transport=httpx.MockTransport(handler))


def test_full_turn_round_trip():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=_sse_wire(TURN1))

    identity = Identity(user_id="TEST0001", token_provider=LocalJwtMinter("TEST0001"))
    session = Session(_make_transport(handler), identity)
    rec = session.ask("Suggest skills I should add to my profile")

    # auth + request shape
    assert captured["auth"].startswith("Bearer ")
    assert captured["body"]["messages"][-1]["content"].startswith("Suggest skills")
    assert "threadId" in captured["body"] and "runId" in captured["body"]

    # normalized record
    assert rec.completion_status == CompletionStatus.COMPLETED
    assert rec.stream_health.run_started_seen and rec.stream_health.run_finished_seen
    assert rec.assistant_text == "Here are your skills."
    assert rec.tool_names() == ["suggest_skills"]
    assert rec.tool_calls[0].result == {"top": [], "additional": []}
    assert rec.timing.ttft_ms is not None and rec.timing.total_ms is not None
    assert rec.timing.ttft_ms <= rec.timing.total_ms
    assert rec.usage.source == UsageSource.ESTIMATED and rec.usage.total_tokens > 0
    assert all(e.arrival_ms is not None for e in rec.events)
    # arrival timestamps are monotonic non-decreasing
    arrivals = [e.arrival_ms for e in rec.events]
    assert arrivals == sorted(arrivals)


def test_multi_turn_session_accumulates():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        # turn 1 has just the user message; later turns carry history
        events = TURN1 if len(body["messages"]) == 1 else TURN2
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=_sse_wire(events))

    identity = Identity(user_id="U", token_provider=LocalJwtMinter("U"))
    session = Session(_make_transport(handler), identity)

    r1 = session.ask("Suggest skills")
    n_after_t1 = len(session.state.messages)
    r2 = session.ask("Save Python and Docker")

    assert r1.turn_index == 0 and r2.turn_index == 1
    assert r2.assistant_text == "Saved."
    assert session.state.thread_id  # unchanged across turns
    assert len(session.state.messages) > n_after_t1  # history grew
    assert n_after_t1 >= 2  # at least user + assistant recorded after turn 1


def test_auth_failure_is_errored_not_raised():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, headers={"content-type": "application/json"},
                              content=b'{"error":"unauthorized"}')

    identity = Identity(user_id="U", token_provider=LocalJwtMinter("U"))
    session = Session(_make_transport(handler), identity)
    rec = session.ask("hello")
    assert rec.completion_status == CompletionStatus.ERRORED
    assert rec.error is not None
