"""Transport contract + session handling.

A ``Transport`` drives ONE turn against the system-under-test and returns a
``RunRecord``. Multi-turn state (threadId, accumulated message history, last
state) lives in ``SessionState``, owned by the caller — keeping ``run_turn``
stateless and easy to retry/parallelize. ``Session`` binds a transport +
identity + state into a ``.ask()`` driver that satisfies ``core.TurnDriver``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from ..core.run_record import RunRecord
from .auth import TokenProvider


class TransportError(RuntimeError):
    """Raised for unrecoverable transport failures (bad config, auth, etc.)."""


@dataclass
class Identity:
    user_id: str
    token_provider: TokenProvider


@dataclass
class TurnRequest:
    user_message: str
    identity: Identity
    tools: list[dict] = field(default_factory=list)
    context: list[dict] = field(default_factory=list)
    forwarded_props: dict = field(default_factory=dict)
    timeout_s: float = 120.0


@dataclass
class SessionState:
    thread_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    messages: list[dict] = field(default_factory=list)  # AG-UI message dicts (history)
    last_state: dict | None = None
    turn_index: int = 0


@runtime_checkable
class Transport(Protocol):
    def run_turn(self, turn: TurnRequest, session: SessionState) -> RunRecord:
        ...


class Session:
    """A single conversation: fixed thread, accumulating history, one identity.

    ``ask()`` returns the normalized ``RunRecord`` for the turn and mutates the
    underlying ``SessionState`` (history + state) so the next turn is coherent.
    """

    def __init__(
        self,
        transport: Transport,
        identity: Identity,
        *,
        state: SessionState | None = None,
        tools: list[dict] | None = None,
        context: list[dict] | None = None,
        forwarded_props: dict | None = None,
        timeout_s: float = 120.0,
    ) -> None:
        self.transport = transport
        self.identity = identity
        self.state = state or SessionState()
        self.tools = tools or []
        self.context = context or []
        self.forwarded_props = forwarded_props or {}
        self.timeout_s = timeout_s

    def ask(self, question: str) -> RunRecord:
        turn = TurnRequest(
            user_message=question,
            identity=self.identity,
            tools=self.tools,
            context=self.context,
            forwarded_props=self.forwarded_props,
            timeout_s=self.timeout_s,
        )
        return self.transport.run_turn(turn, self.state)
