"""Protocol adapter interface.

A ``ProtocolAdapter`` knows how to send a single user message to one agent
endpoint and return a normalized ``PredictResponse``. The runner is unaware
of the underlying wire format (A2A JSON-RPC, ag-ui SSE, etc.).

The adapter also exposes a ``predict_fn`` callable matching the legacy
``predict_fn(question, context_id=..., **kwargs)`` signature used by chat-evals'
``HRBenchmarker``, so the existing MLflow runner code can drive any protocol
without modification.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable

from .trace import Trace


@dataclass
class PredictRequest:
    """A single request to the agent."""

    question: str
    thread_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class PredictResponse:
    """A single response from the agent.

    Compatible with chat-evals' ``A2AResponse`` so existing scorers that
    expect ``text``, ``trace``, ``artifacts``, ``metadata``, ``state`` columns
    on the eval row keep working.
    """

    text: str
    trace: Trace = field(default_factory=Trace)
    artifacts: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    state: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class ProtocolAdapter(ABC):
    """Send a ``PredictRequest`` to one agent endpoint; return a ``PredictResponse``."""

    @abstractmethod
    def send(self, request: PredictRequest, **kwargs: Any) -> PredictResponse:
        """Send the request and return the structured response.

        Implementations should raise a protocol-specific exception (subclass of
        ``RuntimeError``) on transport / parsing failures.
        """

    def predict_fn(self) -> Callable[..., Any]:
        """Return an MLflow-style predict function.

        The returned callable matches the ``HRBenchmarker.predict_fn`` contract:
        ``predict_fn(question: str, context_id: str | None = None, **kwargs)``
        returns a ``PredictResponse``.
        """

        def fn(question: str, context_id: str | None = None, **kwargs: Any) -> PredictResponse:
            return self.send(PredictRequest(question=question, thread_id=context_id), **kwargs)

        return fn

    def new_thread_id(self) -> str:
        """Return a fresh thread / context id for a new conversation.

        Default: a random UUID4. Adapters that need server-side thread creation
        (e.g. A2A via BFF GraphQL) should override.
        """
        import uuid

        return str(uuid.uuid4())
