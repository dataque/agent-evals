"""End-to-end transport path offline: AgUiSseTransport against a mock backend.

Proves the full chain — create the BFF thread (GraphQL) → build RunAgentInput →
POST → parse SSE (incl. the leading-space wire quirk) → reduce → timing → usage
→ RunRecord — plus multi-turn session accumulation and the auth-failure path.
"""

from __future__ import annotations

import json

import httpx

from agent_evals.core.run_record import CompletionStatus, UsageSource
from agent_evals.transport import AgUiSseTransport, Identity, LocalJwtMinter, Session

_THREAD_ID = "T-created-123"


def _sse_wire(events: list[dict]) -> bytes:
    # Reproduce the backend quirk: each data value is prefixed with a space, so
    # after SSE strips one leading space a residual one remains for lstrip().
    buf = ""
    for e in events:
        buf += "data:  " + json.dumps(e) + "\n\n"
    return buf.encode("utf-8")


def _create_session_response() -> httpx.Response:
    # branch-aware backend: createThread was replaced by createSession
    return httpx.Response(200, json={"data": {"createSession": {"id": _THREAD_ID}}})


# New-wire turn: skills tools ack via the {status, data:{result}} envelope, the
# skills payload rides STATE_SNAPSHOT, and pills arrive as a NEXT_STEPS CUSTOM
# event before RUN_FINISHED.
TURN1 = [
    {"type": "RUN_STARTED", "timestamp": 1},
    {"type": "TEXT_MESSAGE_START", "messageId": "m1", "role": "assistant", "timestamp": 2},
    {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "Here are your skills.", "timestamp": 3},
    {"type": "TEXT_MESSAGE_END", "messageId": "m1", "timestamp": 4},
    {"type": "TOOL_CALL_START", "toolCallId": "tc1", "toolCallName": "suggest_skills", "timestamp": 5},
    {"type": "TOOL_CALL_ARGS", "toolCallId": "tc1", "delta": "{}", "timestamp": 6},
    {"type": "TOOL_CALL_END", "toolCallId": "tc1", "timestamp": 7},
    {"type": "STATE_SNAPSHOT",
     "snapshot": {"skills": {"top": [{"name": "Python", "source": "AI_INFERRED"}], "additional": []}},
     "timestamp": 8},
    {"type": "TOOL_CALL_RESULT", "toolCallId": "tc1",
     "content": '{"status":"SUCCESS","data":{"result":"State property skills updated."}}', "timestamp": 9},
    {"type": "CUSTOM", "name": "NEXT_STEPS",
     "value": [{"id": "p1", "suggestion": "Please save these skills to my profile"}], "timestamp": 10},
    {"type": "RUN_FINISHED", "timestamp": 11},
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


def test_creates_thread_then_runs():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/graphql"):
            captured["graphql_url"] = str(request.url)
            captured["graphql_auth"] = request.headers.get("authorization")
            return _create_session_response()
        captured["sse_auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=_sse_wire(TURN1))

    identity = Identity(user_id="TEST0001", token_provider=LocalJwtMinter("TEST0001"))
    session = Session(_make_transport(handler), identity)
    rec = session.ask("Suggest skills I should add to my profile")

    # thread was created via GraphQL (derived endpoint) with the bearer token,
    # and its id is what the SSE run uses — NOT a random UUID.
    assert captured["graphql_url"] == "http://backend/graphql"
    assert captured["graphql_auth"].startswith("Bearer ")
    assert captured["body"]["threadId"] == _THREAD_ID
    assert rec.thread_id == _THREAD_ID

    assert rec.completion_status == CompletionStatus.COMPLETED
    assert rec.assistant_text == "Here are your skills."
    assert rec.tool_names() == ["suggest_skills"]
    # the new wire lands: state snapshot applied + NEXT_STEPS captured
    assert rec.final_state == {"skills": {"top": [{"name": "Python", "source": "AI_INFERRED"}],
                                          "additional": []}}
    assert any(e.type == "CUSTOM" and (e.payload or {}).get("name") == "NEXT_STEPS"
               for e in rec.events)
    assert rec.usage.source == UsageSource.ESTIMATED and rec.usage.total_tokens > 0
    arrivals = [e.arrival_ms for e in rec.events]
    assert arrivals == sorted(arrivals)


def test_multi_turn_reuses_one_thread():
    calls = {"graphql": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/graphql"):
            calls["graphql"] += 1
            return _create_session_response()
        body = json.loads(request.content)
        events = TURN1 if len(body["messages"]) == 1 else TURN2
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=_sse_wire(events))

    identity = Identity(user_id="U", token_provider=LocalJwtMinter("U"))
    session = Session(_make_transport(handler), identity)

    r1 = session.ask("Suggest skills")
    n_after_t1 = len(session.state.messages)
    r2 = session.ask("Save Python and Docker")

    assert calls["graphql"] == 1                 # thread created once, reused
    assert session.state.thread_id == _THREAD_ID
    assert r1.turn_index == 0 and r2.turn_index == 1
    assert r2.assistant_text == "Saved."
    assert len(session.state.messages) > n_after_t1


def test_create_thread_can_be_disabled():
    def handler(request: httpx.Request) -> httpx.Response:
        assert not request.url.path.endswith("/graphql"), "graphql must not be called"
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=_sse_wire(TURN1))

    t = AgUiSseTransport("http://backend/api/v1/bff/ai/agent/sse",
                         create_thread=False, http_transport=httpx.MockTransport(handler))
    session = Session(t, Identity(user_id="U", token_provider=LocalJwtMinter("U")))
    rec = session.ask("hi")
    assert rec.completion_status == CompletionStatus.COMPLETED  # uses the session's own threadId


def test_drains_late_events_after_run_finished():
    # AG-UI forbids events after RUN_FINISHED, but the backend has raced pills
    # past it before — the transport must drain to EOF instead of breaking at
    # the first RUN_FINISHED, and the run still completes cleanly.
    late = [
        {"type": "RUN_STARTED", "timestamp": 1},
        {"type": "TEXT_MESSAGE_CHUNK", "messageId": "m", "delta": "Done.", "timestamp": 2},
        {"type": "RUN_FINISHED", "timestamp": 3},
        {"type": "CUSTOM", "name": "NEXT_STEPS",
         "value": [{"id": "p1", "suggestion": "Suggest open roles"}], "timestamp": 4},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/graphql"):
            return _create_session_response()
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=_sse_wire(late))

    session = Session(_make_transport(handler), Identity(user_id="U", token_provider=LocalJwtMinter("U")))
    rec = session.ask("save")
    assert rec.completion_status == CompletionStatus.COMPLETED
    custom_names = [(e.payload or {}).get("name") for e in rec.events if e.type == "CUSTOM"]
    assert "NEXT_STEPS" in custom_names


def test_create_session_failure_falls_back_to_lazy_creation():
    # createSession fails (e.g. GraphQL error) but the run itself works: the
    # orchestrator lazily creates a session for the unknown threadId, so the
    # turn completes cleanly with the harness's own thread id.
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/graphql"):
            return httpx.Response(200, json={"errors": [{"message": "boom"}]})
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=_sse_wire(TURN2))

    session = Session(_make_transport(handler), Identity(user_id="U", token_provider=LocalJwtMinter("U")))
    rec = session.ask("hello")
    assert rec.completion_status == CompletionStatus.COMPLETED
    assert rec.error is None
    assert captured["body"]["threadId"] == session.state.thread_id != _THREAD_ID


def test_create_session_and_run_failure_is_errored():
    # both the explicit create AND the run fail -> errored RunRecord carrying
    # the createSession context, not a raise
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    session = Session(_make_transport(handler), Identity(user_id="U", token_provider=LocalJwtMinter("U")))
    rec = session.ask("hello")
    assert rec.completion_status == CompletionStatus.ERRORED
    assert rec.error is not None and "createSession" in rec.error.message
