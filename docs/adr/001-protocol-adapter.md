# ADR 001 — Protocol adapter abstraction

**Status**: Accepted (Phase 1, 2026-05).

## Context

The HR Agent PoC at `/Users/neo/projects/chat-evals/` evaluates a single agent over a single protocol (A2A JSON-RPC). The production target, `backend`, exposes two protocols: A2A (`/api/v1/bff/ai/agent/a2a`) and ag-ui SSE (`/api/v1/bff/ai/agent/sse`). A future project ("frontend with primary SSE eval") and other tool-calling agents (open-question: a Slack-based agent) will need additional protocols.

If the runner were coupled to one wire format we'd accumulate protocol-specific runners (one per format × project), each maintaining its own trace parser, thread-management, and authentication wiring. The proven scorer set (built-in MLflow scorers + 7 custom trace-aware scorers from chat-evals) is protocol-agnostic — it consumes a normalized trace shape — and we want to keep it that way.

## Decision

Introduce a single `ProtocolAdapter` ABC in `agent_evals.core.protocol`:

```python
class ProtocolAdapter(ABC):
    @abstractmethod
    def send(self, request: PredictRequest, **kwargs) -> PredictResponse: ...
    def new_thread_id(self) -> str: ...
```

with normalized `PredictRequest` / `PredictResponse` dataclasses. `PredictResponse` carries `text`, `trace` (a normalized `Trace`), `artifacts`, `metadata`, `state`, mirroring chat-evals' `A2AResponse` shape so scorers ported from chat-evals consume rows produced by either adapter unchanged.

Phase 1 ships one concrete adapter: `A2AAdapter` (in `agent_evals/protocols/a2a/`) wrapping a verbatim port of the chat-evals A2A client logic. Phase 3 will add `AGUIAdapter` for the ag-ui SSE protocol.

The `MLflowRunner` depends only on the abstract interface, not concrete adapters.

## Consequences

**Positive:**
- Scorers are protocol-agnostic. New protocols add ~one adapter, not a forked runner.
- Trace shape is normalized at the adapter boundary, not at scorer time. Each adapter is the single source of truth for "how do my events become a `Trace`".
- The chat-evals scorer port is verbatim — no protocol-specific branches in scorer code.
- `predict_fn` callable view (legacy) is preserved via `ProtocolAdapter.predict_fn()`, so any chat-evals-style runner code keeps working during the migration.

**Negative:**
- One extra indirection vs calling the A2A client directly. Acceptable — the indirection is a typed contract, not a behavioural change.
- The normalized `Trace` shape is currently the chat-evals v1 trace dict (events list). When ag-ui ships, we either project ag-ui events onto the same dict shape OR generalise `Trace` to a richer event model. Either path is open; locking in v1 buys us scorer compatibility today.

## Alternatives considered

1. **Skip the abstraction, write `A2ARunner` and `AGUIRunner` separately.** Rejected — duplicates the scorer-row-construction logic, the multi-turn loop, the Azure OpenAI judge wiring, and the hyperparameter grid expansion. Three runners' worth of code for one runner's worth of value.

2. **Reuse `mlflow.genai.evaluate`'s `predict_fn` as the only abstraction.** Rejected — `predict_fn` returns one value, conventionally a string. We need typed access to trace + artifacts + metadata for the trace-aware scorers, which is what `PredictResponse` provides. `predict_fn` is still exposed via `ProtocolAdapter.predict_fn()` for callers that need only text.

3. **Make `Trace` a generic event-stream type up front.** Rejected as premature for Phase 1. ag-ui has its own event taxonomy we haven't fully mapped yet; locking in a generic shape before having two concrete consumers is speculative. Defer to the AGUI plan.
