"""Normalized Trace data model.

The framework's protocol-agnostic representation of "what the agent did" during
a single request. Concrete protocol adapters (A2A, AGUI, ...) build a ``Trace``
from their wire format; scorers consume it without caring which protocol
produced it.

The shape is intentionally compatible with the ``hr-agent/Trace@v1`` artifact
chat-evals already emits, so the trace-aware scorers ported from chat-evals
keep their semantics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass
class Event:
    """A single timestamped event in a trace."""

    type: str
    data: Mapping[str, Any] = field(default_factory=dict)
    timestamp: int | None = None


@dataclass
class ToolCall:
    """A tool invocation as observed in the trace."""

    tool_name: str
    args: Mapping[str, Any] = field(default_factory=dict)
    call_id: str | None = None
    timestamp: int | None = None


@dataclass
class ToolResult:
    """The result of a tool invocation."""

    tool_name: str
    status: str
    result: Any = None
    call_id: str | None = None
    timestamp: int | None = None


@dataclass
class Trace:
    """Normalized trace produced by a protocol adapter.

    ``raw`` carries the original ``hr-agent/Trace@v1``-shaped dict so trace-aware
    scorers ported from chat-evals (which read ``events: list[dict]``) keep
    working unchanged. The structured fields (``tool_calls``, ``tool_results``,
    ``routes``) are derived views for code that prefers typed access.
    """

    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: Any) -> "Trace":
        """Build a ``Trace`` from a v1 trace dict (or empty for missing trace)."""
        if isinstance(payload, dict):
            return cls(raw=payload)
        return cls(raw={})

    # ------------------------------------------------------------------
    # Event accessors — ported from chat-evals/evals/scorers.py:59-87.
    # These were free functions there; now they are methods so scorers can
    # depend on the Trace contract directly.
    # ------------------------------------------------------------------

    def events(self) -> list[dict[str, Any]]:
        return list(self.raw.get("events", []) or [])

    def tool_calls(self) -> list[dict[str, Any]]:
        return [e for e in self.events() if e.get("type") == "tool_call"]

    def tool_results(self) -> list[dict[str, Any]]:
        return [e for e in self.events() if e.get("type") == "tool_result"]

    def routes(self) -> list[dict[str, Any]]:
        return [e for e in self.events() if e.get("type") == "route"]

    # ------------------------------------------------------------------
    # Metadata views
    # ------------------------------------------------------------------

    def metadata(self) -> dict[str, Any]:
        return dict(self.raw.get("metadata") or {})

    def agent_token_totals(self) -> dict[str, dict[str, int]]:
        return dict(self.raw.get("agent_token_totals", {}) or {})

    def is_empty(self) -> bool:
        return not bool(self.raw)


def f1_score(expected: set[Any], observed: set[Any]) -> float:
    """F1 of two sets. Ported from chat-evals/evals/scorers.py:77-87."""
    if not expected and not observed:
        return 1.0
    if not expected or not observed:
        return 0.0
    tp = len(expected & observed)
    if tp == 0:
        return 0.0
    precision = tp / len(observed)
    recall = tp / len(expected)
    return 2 * precision * recall / (precision + recall)
