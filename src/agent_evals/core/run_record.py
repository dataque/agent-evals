"""The transport-neutral normalized result of a single agent run/turn.

Every transport adapter (AG-UI/SSE, A2A, ...) produces a ``RunRecord``; every
scorer consumes one. This is the single seam that makes the eval system both
transport-independent and eval-framework-independent: nothing in this module
imports a transport library or a metrics backend.

The field set is a superset of what the 24 in-scope metrics in ``docs/metrics.md``
require, so that adding a metric never forces a transport change.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class UsageSource(str, Enum):
    REPORTED = "reported"            # backend emitted real token/cost usage
    ESTIMATED = "estimated"          # tokenizer estimate over prompts + completions
    UNKNOWN = "unknown"              # neither available


class CompletionStatus(str, Enum):
    COMPLETED = "completed"              # RUN_FINISHED seen, no error
    ERRORED = "errored"                  # RUN_ERROR seen
    ABORTED_TIMEOUT = "aborted_timeout"  # wall-clock / idle timeout before RUN_FINISHED
    TRUNCATED = "truncated"              # stream closed before RUN_FINISHED, no error


class ToolStatus(str, Enum):
    OK = "ok"                        # start + end + result present, args parsed
    ERROR = "error"                  # result content indicates an error
    INCOMPLETE = "incomplete"        # START seen but no END at stream close
    MISSING_RESULT = "missing_result"  # END seen but no RESULT at stream close
    ORPHAN_RESULT = "orphan_result"  # RESULT with no matching START
    BAD_ARGS = "bad_args"            # END present but args JSON unparseable


class NormalizedMessage(BaseModel):
    """One message in canonical AG-UI shape, transport-neutral."""

    id: str
    role: str  # user | assistant | tool | system | developer | reasoning
    content: str | None = None
    name: str | None = None
    tool_calls: list[dict] | None = None
    tool_call_id: str | None = None


class Event(BaseModel):
    """One raw protocol event with client-observed timing.

    The ordered list of these is the source of truth for stream-health (#24)
    and latency (#13). ``arrival_ms`` is the client monotonic clock, NOT the
    server-side construction timestamp (which is unreliable for latency).
    """

    seq: int
    type: str
    arrival_ms: float | None = None       # ms since request start (client monotonic clock)
    arrival_wall: float | None = None     # epoch seconds at client arrival
    server_timestamp: int | None = None   # backend-side construction timestamp (advisory)
    payload: dict = Field(default_factory=dict)
    raw: dict = Field(default_factory=dict)


class ToolCall(BaseModel):
    """One tool invocation, assembled from START/ARGS/END + later RESULT (SSE)
    or from a trace tool_call/tool_result pair (A2A)."""

    tool_call_id: str
    name: str | None = None
    args: dict | None = None
    args_raw: str | None = None
    args_parse_error: str | None = None
    result: Any | None = None
    result_raw: str | None = None
    status: ToolStatus = ToolStatus.OK
    is_error: bool = False
    parent_message_id: str | None = None
    result_message_id: str | None = None
    result_role: str | None = None
    started_arrival_ms: float | None = None
    ended_arrival_ms: float | None = None
    result_arrival_ms: float | None = None
    owning_step_index: int | None = None
    owning_subagent: str | None = None


class Step(BaseModel):
    """A STEP_STARTED/STEP_FINISHED span — marks subagent/phase boundaries."""

    name: str | None = None
    started_arrival_ms: float | None = None
    ended_arrival_ms: float | None = None


class SubagentRoute(BaseModel):
    """A derived routing decision. SSE: synthesized from Task tool calls / step
    names. A2A: from native route events."""

    subagent: str
    via: str  # task_tool | step_name | route_event
    arrival_ms: float | None = None


class ReasoningSegment(BaseModel):
    kind: str  # reasoning | thinking
    text: str
    started_arrival_ms: float | None = None
    ended_arrival_ms: float | None = None


class TokenUsage(BaseModel):
    """Token/cost. May be reported, estimated, or unknown — always flagged via
    ``source`` so dashboards can distinguish estimated from real values."""

    source: UsageSource = UsageSource.UNKNOWN
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None
    cost_rates_version: str | None = None
    by_subagent: dict[str, dict[str, int]] | None = None
    by_model: dict[str, dict[str, int]] | None = None
    estimator: str | None = None


class StreamHealth(BaseModel):
    """Protocol-invariant diagnostics feeding metric #24. Stores raw breaches,
    not a score — the scorer decides policy. A clean stream has empty lists and
    both lifecycle flags true."""

    run_started_seen: bool = False
    run_finished_seen: bool = False
    ended_before_finished: bool = False
    duplicate_run_started: bool = False
    ordering_violations: list[str] = Field(default_factory=list)
    unmatched_tool_starts: list[str] = Field(default_factory=list)
    unmatched_tool_ends: list[str] = Field(default_factory=list)
    tool_calls_missing_result: list[str] = Field(default_factory=list)
    orphan_tool_results: list[str] = Field(default_factory=list)
    malformed_arg_tool_calls: list[str] = Field(default_factory=list)
    state_patch_errors: list[str] = Field(default_factory=list)
    unknown_event_types: list[str] = Field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return (
            self.run_started_seen
            and self.run_finished_seen
            and not self.ended_before_finished
            and not self.duplicate_run_started
            and not self.ordering_violations
            and not self.unmatched_tool_starts
            and not self.unmatched_tool_ends
            and not self.tool_calls_missing_result
            and not self.orphan_tool_results
            and not self.malformed_arg_tool_calls
            and not self.state_patch_errors
        )


class DerivedTiming(BaseModel):
    """Client-observed timing feeding metric #13. Per-run primitives only;
    P50/P95/P99 are computed across many runs by ``core.aggregate``."""

    ttft_ms: float | None = None              # first content-bearing event
    total_ms: float | None = None             # RUN_FINISHED (or last event if aborted)
    request_to_run_started_ms: float | None = None
    per_event_type_first_ms: dict[str, float] = Field(default_factory=dict)
    per_event_type_count: dict[str, int] = Field(default_factory=dict)
    tool_latencies_ms: dict[str, float] = Field(default_factory=dict)
    aborted: bool = False


class RunError(BaseModel):
    message: str
    code: str | None = None
    arrival_ms: float | None = None


class RunRecord(BaseModel):
    """Normalized result of ONE agent turn. The single structure all scorers
    consume (directly or via a backend-specific projection)."""

    # Identity / correlation
    thread_id: str
    run_id: str
    turn_index: int = 0
    user_id: str | None = None
    transport: str = "agui_sse"

    # Input echoed for scorer convenience
    user_message: str = ""

    # Primary outputs
    assistant_text: str = ""  # "" for a tool-only turn
    messages: list[NormalizedMessage] = Field(default_factory=list)
    tool_calls: list[ToolCall] = Field(default_factory=list)
    reasoning: list[ReasoningSegment] = Field(default_factory=list)
    steps: list[Step] = Field(default_factory=list)
    subagent_routes: list[SubagentRoute] = Field(default_factory=list)
    final_state: dict | None = None

    # Observability
    events: list[Event] = Field(default_factory=list)
    timing: DerivedTiming = Field(default_factory=DerivedTiming)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    stream_health: StreamHealth = Field(default_factory=StreamHealth)
    completion_status: CompletionStatus = CompletionStatus.COMPLETED
    error: RunError | None = None

    # Artifact handle
    raw_transcript_ref: str | None = None

    # --- convenience accessors -------------------------------------------
    def tool_names(self) -> list[str]:
        return [tc.name for tc in self.tool_calls if tc.name]

    def calls_to(self, name: str) -> list[ToolCall]:
        return [tc for tc in self.tool_calls if tc.name == name]

    @property
    def succeeded(self) -> bool:
        return self.completion_status == CompletionStatus.COMPLETED and self.error is None
