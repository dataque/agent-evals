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
        http_transport: "httpx.BaseTransport | None" = None,
    ) -> None:
        self.url = url
        self.headers = headers or {}
        self.encoding = encoding
        self.connect_timeout_s = connect_timeout_s
        self.persist_dir = Path(persist_dir) if persist_dir else None
        self.verify = verify
        # Optional injected httpx transport (e.g. MockTransport for tests,
        # or a retrying/proxy transport in production).
        self.http_transport = http_transport

    # ------------------------------------------------------------------
    def run_turn(self, turn: TurnRequest, session: SessionState) -> RunRecord:
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
        try:
            token = turn.identity.token_provider.get_token()
        except Exception as exc:  # token providers can fail (refresh, etc.)
            raise TransportError(f"token provider failed: {exc}") from exc
        headers = {"Authorization": f"Bearer {token}", **self.headers}

        events = []
        t0 = time.perf_counter()
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
                        if ev.type == ET.RUN_FINISHED:
                            break
                        if (time.perf_counter() - t0) > turn.timeout_s:
                            aborted_timeout = True
                            break
        except httpx.HTTPStatusError as exc:
            transport_error = f"HTTP {exc.response.status_code}"
        except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.PoolTimeout):
            aborted_timeout = True
        except Exception as exc:
            transport_error = f"{type(exc).__name__}: {exc}"

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
    def _persist(self, events: list, run_id: str) -> str | None:
        if not self.persist_dir:
            return None
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        path = self.persist_dir / f"{run_id}.json"
        path.write_text(json.dumps([e.raw for e in events], ensure_ascii=False, default=str))
        return str(path)
