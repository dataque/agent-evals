"""The primary transport: drive the backend's AG-UI-over-SSE endpoint and
normalize the stream into a ``RunRecord``.

This module is the only place that does network I/O; all parsing/timing/usage
logic lives in pure modules (``events``/``reducer``/``timing``/``usage``) so the
adapter is a thin, testable shell.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from httpx_sse import connect_sse

from ...core.run_record import CompletionStatus, RunError, RunRecord
from ..base import SessionState, TransportError, TurnRequest
from .events import ET, parse_event
from .reducer import reduce_events
from .timing import derive_timing
from .usage import compute_usage


class AgUiSseTransport:
    """AG-UI ``RunAgentInput`` over POST → ``text/event-stream`` → ``RunRecord``."""

    def __init__(
        self,
        url: str,
        *,
        headers: dict | None = None,
        encoding: str = "cl100k_base",
        connect_timeout_s: float = 15.0,
        persist_dir: str | None = None,
        verify: "bool | str" = True,  # True | False (insecure) | path to a CA bundle (.pem)
        create_thread: bool = True,
        graphql_url: str | None = None,
        http_transport: "httpx.BaseTransport | None" = None,
        post_finish_grace_s: float = 10.0,
    ) -> None:
        self.url = url
        self.headers = headers or {}
        self.encoding = encoding
        self.connect_timeout_s = connect_timeout_s
        self.persist_dir = Path(persist_dir) if persist_dir else None
        self.verify = verify
        # Mirror the frontend flow: create a chat session via the GraphQL
        # `createSession` mutation before the first turn (the backend's
        # branch-aware rework renamed the old `createThread`). Best-effort —
        # the orchestrator now also lazily creates a session for an unknown
        # threadId, so a failed explicit create degrades to lazy creation
        # instead of aborting the turn. Set False to skip the explicit create;
        # ``graphql_url`` overrides the derived endpoint.
        self.create_thread = create_thread
        self.graphql_url = graphql_url
        # AG-UI forbids events after RUN_FINISHED, but the backend has raced late
        # events (follow-up pills) past it before. Instead of breaking at
        # RUN_FINISHED we keep draining until EOF (the normal case — the server
        # closes the stream) or this grace window, so a late NEXT_STEPS / STATE
        # event is still captured.
        self.post_finish_grace_s = post_finish_grace_s
        # Optional injected httpx transport (e.g. MockTransport for tests,
        # or a retrying/proxy transport in production).
        self.http_transport = http_transport

    # ------------------------------------------------------------------
    def run_turn(self, turn: TurnRequest, session: SessionState) -> RunRecord:
        try:
            token = turn.identity.token_provider.get_token()
        except Exception as exc:  # token providers can fail (refresh, etc.)
            raise TransportError(f"token provider failed: {exc}") from exc

        # Create the chat session on the first turn (later turns reuse it). A
        # failure here is survivable: the orchestrator lazily creates a session
        # for an unknown threadId, so we fall back to the session's own id and
        # only surface the create error if the run itself also fails.
        create_error: str | None = None
        if self.create_thread and session.turn_index == 0:
            try:
                session.thread_id = self._create_session(token)
            except Exception as exc:
                create_error = f"createSession failed: {type(exc).__name__}: {exc}"

        run_id = str(uuid.uuid4())
        user_msg_id = str(uuid.uuid4())
        body = {
            "threadId": session.thread_id,
            "runId": run_id,
            "messages": [
                *session.messages,
                {"id": user_msg_id, "role": "user", "content": turn.user_message},
            ],
            "tools": turn.tools,
            "context": turn.context,
            "state": session.last_state or {},
            "forwardedProps": turn.forwarded_props or {},
        }
        headers = {"Authorization": f"Bearer {token}", **self.headers}

        events = []
        t0 = time.perf_counter()
        finished_at: float | None = None
        aborted_timeout = False
        transport_error: str | None = None
        timeout = httpx.Timeout(turn.timeout_s, connect=self.connect_timeout_s)
        if self.http_transport is not None:
            client_kwargs = {"timeout": timeout, "transport": self.http_transport}
        else:
            client_kwargs = {"timeout": timeout, "verify": self.verify}

        try:
            with httpx.Client(**client_kwargs) as client:
                with connect_sse(client, "POST", self.url, json=body, headers=headers) as es:
                    for sse in es.iter_sse():
                        now = time.perf_counter()
                        if finished_at is not None and (now - finished_at) > self.post_finish_grace_s:
                            break  # drained the post-RUN_FINISHED grace window
                        if (now - t0) > turn.timeout_s:
                            if finished_at is None:
                                aborted_timeout = True
                            break
                        if not sse.data:
                            continue
                        raw_txt = sse.data.lstrip()  # backend prefixes each payload with a space
                        try:
                            obj = json.loads(raw_txt)
                        except Exception:
                            continue  # skip heartbeats / non-JSON comments
                        if not isinstance(obj, dict):
                            continue
                        ev = parse_event(
                            obj,
                            seq=len(events),
                            arrival_ms=(time.perf_counter() - t0) * 1000.0,
                            arrival_wall=time.time(),
                        )
                        events.append(ev)
                        if ev.type == ET.RUN_FINISHED and finished_at is None:
                            finished_at = time.perf_counter()
        except httpx.HTTPStatusError as exc:
            transport_error = f"HTTP {exc.response.status_code}"
        except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.PoolTimeout):
            # a read timeout AFTER RUN_FINISHED is just an idle drain — clean
            if finished_at is None:
                aborted_timeout = True
        except Exception as exc:
            # stream teardown after RUN_FINISHED (e.g. an abrupt server close
            # mid-drain) must not error an already-completed run
            if finished_at is None:
                transport_error = f"{type(exc).__name__}: {exc}"

        # attribute a failed explicit session-create only when the run itself
        # did not succeed (lazy creation covers the happy path)
        if transport_error and create_error:
            transport_error = f"{create_error}; then {transport_error}"
        elif create_error and not events:
            transport_error = create_error

        reduced = reduce_events(events, aborted_timeout=aborted_timeout)
        timing = derive_timing(
            events,
            reduced.tool_calls,
            aborted=aborted_timeout or reduced.completion_status != CompletionStatus.COMPLETED,
        )
        usage = compute_usage(
            input_messages=body["messages"],
            assistant_text=reduced.assistant_text,
            tool_calls=reduced.tool_calls,
            reasoning=reduced.reasoning,
            events=events,
            final_state=reduced.final_state,
            encoding=self.encoding,
        )

        error = reduced.error
        completion_status = reduced.completion_status
        if transport_error and error is None:
            error = RunError(message=transport_error)
        if transport_error and not events:
            completion_status = CompletionStatus.ERRORED

        rec = RunRecord(
            thread_id=session.thread_id,
            run_id=run_id,
            turn_index=session.turn_index,
            user_id=turn.identity.user_id,
            transport="agui_sse",
            user_message=turn.user_message,
            assistant_text=reduced.assistant_text,
            messages=reduced.messages,
            tool_calls=reduced.tool_calls,
            reasoning=reduced.reasoning,
            steps=reduced.steps,
            subagent_routes=reduced.subagent_routes,
            final_state=reduced.final_state,
            events=events,
            timing=timing,
            usage=usage,
            stream_health=reduced.stream_health,
            completion_status=completion_status,
            error=error,
            raw_transcript_ref=self._persist(events, run_id),
        )

        # advance the session for the next turn
        session.messages.append({"id": user_msg_id, "role": "user", "content": turn.user_message})
        for m in reduced.messages:
            session.messages.append(m.model_dump(exclude_none=True))
        if reduced.final_state is not None:
            session.last_state = reduced.final_state
        session.turn_index += 1
        return rec

    # ------------------------------------------------------------------
    def _graphql_url(self) -> str:
        if self.graphql_url:
            return self.graphql_url
        if "/api/" in self.url:  # strip the API path, keep any gateway base path
            return self.url.split("/api/", 1)[0] + "/graphql"
        parts = urlsplit(self.url)
        return f"{parts.scheme}://{parts.netloc}/graphql"

    def _create_session(self, token: str) -> str:
        """Create a chat session via the BFF GraphQL endpoint and return its id.

        The backend's branch-aware rework replaced the ``createThread`` mutation
        with ``createSession`` (no arguments — the user comes from the JWT; it
        returns ``Session { id ... }``). The run body still speaks AG-UI
        ``threadId``; the orchestrator resolves it to a session id.
        """
        mutation = "mutation CreateSession { createSession { id } }"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            **self.headers,
        }
        timeout = httpx.Timeout(self.connect_timeout_s + 30.0, connect=self.connect_timeout_s)
        if self.http_transport is not None:
            client_kwargs = {"timeout": timeout, "transport": self.http_transport}
        else:
            client_kwargs = {"timeout": timeout, "verify": self.verify}
        with httpx.Client(**client_kwargs) as client:
            resp = client.post(self._graphql_url(), json={"query": mutation}, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        errors = (data or {}).get("errors")
        if errors:
            # GraphQL transports errors in-band with HTTP 200 — surface them
            raise TransportError(f"createSession returned errors: {errors}")
        session_id = (((data or {}).get("data") or {}).get("createSession") or {}).get("id")
        if not session_id:
            raise TransportError(f"createSession returned no id (response: {data})")
        return session_id

    # ------------------------------------------------------------------
    def _persist(self, events: list, run_id: str) -> str | None:
        if not self.persist_dir:
            return None
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        path = self.persist_dir / f"{run_id}.json"
        path.write_text(json.dumps([e.raw for e in events], ensure_ascii=False, default=str))
        return str(path)
