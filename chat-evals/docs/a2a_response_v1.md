# hr-agent A2A Response Specification v1 — Eval Consumer's View

**Status:** v1.0 — implemented on the FA endpoint. BFF (backend `/api/v1/bff/ai/agent/a2a`) is targeted to converge in Phase 2b.
**Audience:** people writing scorers, datasets, smoke tests, or anything else that consumes the HR Agent's A2A response.
**Source of truth.** Protocol-level fields are mirrored from `hr-agent/docs/a2a_response_v1.md`. This copy adds eval-specific consumer guidance (which scorer reads which field, schema validation, dataset annotation, BFF gaps). Both files are kept in sync — protocol changes land in hr-agent first, then are mirrored here in the same PR.

---

## 1. Overview

The hr-agent service speaks the A2A JSON-RPC protocol (`message/send`, `message/stream`, `tasks/get`, `tasks/cancel`). Its response is an A2A `Task` — but with an opinionated, documented shape designed to carry everything an evaluation harness, observability backend, or downstream agent needs **inline**, without an external trace store.

The shape is **additive over the A2A spec** — every field is either a standard A2A field or a `hr-agent.`-namespaced custom key inside a spec-defined metadata/data slot. Existing text-only callers (e.g. `extract_text` in `evals/hr_benchmarker/a2a_client.py`) keep working unchanged.

### 1.1 Design principles

- **Spec-idiomatic.** Trace lives in an Artifact, not in `Task.history`. Custom keys are namespaced. No invented top-level fields.
- **Self-contained.** No fields require an external store to interpret — `trace_id` is task-scoped. **Eval scorers do NOT need MLflow tracing or an OTel backend to read trace data.**
- **Forward compatible.** Consumers MUST ignore unknown metadata keys, unknown event types, and unknown artifact names.
- **One schema, two implementations.** FA (Python) and BFF (Java) emit the same shape. BFF lags in v1 — see §11.

### 1.2 Where each scoring signal lives

| Eval signal | Field | Scorer(s) consuming it |
|---|---|---|
| Final assistant text | `result.status.message.parts[0].text` (also `A2AResponse.text`) | `Correctness`, `RelevanceToQuery`, `Safety`, `Guidelines`, `response_completeness` |
| Tool calls + results | `execution_trace.events[type ∈ {tool_call, tool_result}]` (also `A2AResponse.events`) | `tool_trace_f1`, `tool_argument_correctness`, `audit_log_action_taken` |
| Step ordering / count | `execution_trace.events` ordered by `sequence` | `step_efficiency`, `plan_quality` |
| Sub-agent attribution | `events[*].agent_id`, `agent_token_totals` | `plan_quality` (via routes); custom scorers |
| Card / format outputs | `result.artifacts[name=...].parts[0].data.schema` | `card_format_correctness` |
| Latency | `metadata.hr-agent.latency_ms` (also `A2AResponse.latency_ms`) | (custom) |
| Tokens | `metadata.hr-agent.tokens` (also `A2AResponse.tokens`) | (custom) |
| Cost | `metadata.hr-agent.cost.usd` (also `A2AResponse.cost_usd`) | (custom) |
| Refusal vs. failure | `state == "completed"` + `artifacts.refusal` vs `state == "failed"` | branch logic in custom scorers |
| HITL audit | `events[type ∈ {interrupt, resume}]` | (custom) |

---

## 2. JSON-RPC envelope

Standard. The response always wraps a Task as `result`:

```jsonc
{
  "jsonrpc": "2.0",
  "id": "<request-id>",
  "result": { /* Task */ }
}
```

JSON-RPC errors (transport-layer or unhandled exceptions) come back as a normal JSON-RPC `error` object. `_parse_response` raises `A2ARequestError` for these. Application-level failures use `state="failed"` and are returned as a normal Task — see §8.3.

---

## 3. Task — top-level fields

| Field | Type | Notes |
|---|---|---|
| `id` | string | Server-issued task id. |
| `contextId` | string | Conversation/thread id; reuse for multi-turn (the harness's `thread_factory` returns one of these per scenario). |
| `kind` | `"task"` | Constant. |
| `status` | TaskStatus | See §4. |
| `history` | Message[] | See §5. Conversational only — never per-step trace. |
| `artifacts` | Artifact[] | Always contains `execution_trace`; may contain named outputs (§6.2) and `refusal` (§6.3). |
| `metadata` | object | All custom keys prefixed `hr-agent.`. See §7. |

The `A2AResponse` dataclass (`evals/hr_benchmarker/a2a_client.py`) decomposes this into named attributes — most scorers consume `A2AResponse` properties rather than the raw JSON, but both are equivalent.

---

## 4. `status.message` — the final reply

For terminal states, `status.message` carries the user-facing response.

```jsonc
{
  "kind": "message",
  "messageId": "<uuid>",
  "role": "agent",
  "parts": [
    { "kind": "text", "text": "<final answer>" }
    /* On state="failed": also a DataPart with hr-agent/Error@v1 — see §8.3 */
    /* On state="input_required": also a DataPart describing the interrupt — see §8.2 */
  ],
  "metadata": {
    "hr-agent.agent_id": "<agent that produced this reply>"
  }
}
```

`status.state` is one of: `submitted`, `working`, `input_required`, `completed`, `failed`, `canceled`. The A2A SDK serialises `input_required`/`auth_required` over the wire as `input-required`/`auth-required` — `_parse_response` normalises both forms by replacing hyphens with underscores in `A2AResponse.state`.

---

## 5. `history`

`history` is reserved for conversation-level Messages: the user's request, the final agent message, HITL interrupt and resume messages. It is **not** a per-step trace — see §6.1.

The A2A SDK only appends to `history` as a side effect of a `TaskStatusUpdateEvent` whose `status.message` is set, so packing per-step events here would require emitting one synthetic `working` status per step — explicitly an anti-pattern under the A2A spec.

**Implication for scorers:** never read `history` for tool calls or routing. Always read `execution_trace.events`.

---

## 6. Artifacts

### 6.1 `execution_trace` — the canonical per-step trace

Always present, even on failure. Carried on a single Artifact:

```jsonc
{
  "artifactId": "<uuid>",
  "name": "execution_trace",
  "description": "Chronological trace of orchestrator and sub-agent steps",
  "parts": [{ "kind": "data", "data": { /* Trace@v1 */ } }],
  "metadata": { "hr-agent.streamable": true }
}
```

`Trace@v1` payload (also accessible via `A2AResponse.trace`):

```jsonc
{
  "schema": "hr-agent/Trace@v1",
  "trace_id": "<task-scoped uuid>",
  "events": [ /* TraceEvent[], ordered by `sequence` */ ],
  "agent_token_totals": {
    "<agent_id>": { "input": <int>, "output": <int> }
  }
}
```

#### 6.1.1 TraceEvent

```jsonc
{
  "span_id": "<uuid>",
  "parent_span_id": "<uuid> | null",
  "sequence": <int, monotonic>,
  "type": "<one of the closed v1 set>",
  "agent_id": "<orchestrator | profile | job_discovery | outreach | ...>",
  "timestamp": "<ISO-8601 UTC>",
  "data": { /* type-specific */ }
}
```

**Span model.** OpenTelemetry-style: `trace_id` per task, `span_id` per step, `parent_span_id` for nesting. A `tool_call` and its paired `tool_result` **share a `span_id`**. `sequence` orders events when timestamps tie. Survives parallel calls, retries, and arbitrary sub-agent nesting.

#### 6.1.2 Event `type` vocabulary (closed v1 set)

Scorers MUST tolerate unknown values (skip / ignore). Adding new types is additive.

| `type` | When emitted | `data` schema |
|---|---|---|
| `route` | Orchestrator delegates to a sub-agent | `{ route_to: <agent_id>, reason: <str>, tool_call_id: <str> }` |
| `reasoning` | Agent natural-language thinking step | `{ text: <str>, model_id: <str \| null> }` |
| `tool_call` | Agent invokes a tool | `{ tool_call_id: <str>, attempt: <int>, tool_name: <str>, args: <object> }` |
| `tool_result` | Tool returns | `{ tool_call_id: <str>, tool_name: <str>, status: "ok" \| "error", result: <any \| null>, error: <any \| null> }` |
| `agent_message` | Sub-agent's intermediate user-facing text (not the final) | `{ text: <str>, model_id: <str \| null> }` |
| `handoff` | Sub-agent returns control to its caller | `{ from_agent: <agent_id>, to_agent: <agent_id>, summary: <str> }` |
| `interrupt` | Entering `input_required` | `{ interrupts: [...], interrupt_agent: <agent_id>, expected_decision_schema?: <str> }` |
| `resume` | HITL decision processed on the next turn | `{ decision: <object> }` |

#### 6.1.3 Pairing rules (read this before writing a tool-trace scorer)

- One `tool_call` corresponds to **exactly one** `tool_result`. They share `span_id` and `tool_call_id`. The id is the LangChain/SDK-issued id, copied verbatim.
- Retries surface as a fresh `tool_call`/`tool_result` pair with a new `span_id` and `attempt: N` (default `0`).
- Args are atomic — there is no streaming/partial `tool_call` event in v1.

#### 6.1.4 `agent_token_totals`

Sum of `usage_metadata` across all AIMessages emitted by each agent during this task. **Per-step token attribution is not in v1** — if a scorer wants per-step tokens, it should be deferred to v1.1 rather than approximated.

### 6.2 Named output artifacts

Stable, schema-versioned outputs — surface in `A2AResponse.artifacts` as a name→data map. v1 reserves these names:

| `name` | `data.schema` | Notes for scorers |
|---|---|---|
| `execution_trace` | `hr-agent/Trace@v1` | Already pulled out as `A2AResponse.trace`; not in `artifacts`. |
| `matched_jobs` | `hr-agent/JobCard@v1` | Use for `card_format_correctness` and structured job-list assertions. |
| `profile_score` | `hr-agent/ProfileScore@v1` | |
| `inferred_skills` | `hr-agent/SkillsCard@v1` | |
| `draft_message` | `hr-agent/DraftMessage@v1` | Outreach draft awaiting send. |
| `refusal` | `hr-agent/Refusal@v1` | See §6.3 / §8.4. |

Adding a new artifact name is additive. The JSON Schema fixture (`evals/schemas/a2a_response.v1.json`) accepts any name; document it here when it stabilises.

### 6.3 Refusal artifact

When the agent successfully runs but declines (policy, scope, safety), the response is `state="completed"` plus a `refusal` artifact:

```jsonc
{
  "name": "refusal",
  "parts": [{ "kind": "data", "data": {
    "schema": "hr-agent/Refusal@v1",
    "reason": "<short rationale>",
    "policy": "<optional policy id>"
  }}]
}
```

This lets scorers detect refusals without NLP. **Always branch on `state` first**, then on the presence of the `refusal` artifact — see §8.4.

---

## 7. `task.metadata` — key registry

All custom keys are prefixed `hr-agent.`. Scorers MUST ignore unknown keys.

| Key | Type | Presence | Semantics |
|---|---|---|---|
| `hr-agent.schema_version` | string | always | `"1.0"` for this version. |
| `hr-agent.latency_ms` | int ≥ 0 | always | Wall-clock, in ms. |
| `hr-agent.tokens` | `{input, output, total}` | always | Sum across orchestrator + workers. |
| `hr-agent.agent_token_totals` | `{<agent_id>: {input, output}}` | always (may be empty) | Per-agent subtotals. |
| `hr-agent.models` | string[] | always (may be empty) | Distinct model_ids observed. |
| `hr-agent.cost` | `{usd, rates_version, by_model}` | only when **all** models in `models` have a rate | See §7.1. |
| `hr-agent.cost_unknown_models` | string[] | only when ≥1 model is unrated | Model ids without a rate; `cost` is omitted. |
| `hr-agent.user_id` | string | when known | Identity actually used for this task (FA: from message metadata; BFF: from JWT). |
| `hr-agent.identity_source` | enum | always | `"a2a-message-metadata"`, `"jwt"`, or `"context-id"`. |
| `hr-agent.error` | `{code, type}` | only when `state="failed"` | Mirror of `Error@v1` for cheap top-level filtering. |

### 7.1 Cost — rules

- `cost` is present ONLY when every model in `models` has a rate in the server's pricing table. Don't silently default to zero.
- When ANY model is unrated, `cost` is omitted and `cost_unknown_models` lists the unrated ids. A cost-aware scorer MUST handle both branches.
- `rates_version` (e.g. `"2025-04"`) ties the prices to a versioned table.
- Currency is USD in v1.

---

## 8. States and flows (and how to score each)

### 8.1 `completed`

Terminal happy path. All standard scorers run.

### 8.2 `input_required` — HITL

- `status.state = "input_required"`.
- `status.message.parts`: TextPart (human-readable) + DataPart `{ interrupts, interrupt_agent, interrupt_thread_id }`.
- An `interrupt` TraceEvent is appended to `execution_trace.events`.
- The harness can resume by sending a follow-up `message/send` on the same `contextId` with the decision (DataPart `{type: "approve" | "decline"}`, JSON text, or plain `approve`/`decline`).
- The resumed task's trace begins with a `resume` event.

**Scoring tip:** when a dataset turn expects HITL to be triggered, assert `state == "input_required"` AND the presence of an `interrupt` event whose `data.interrupt_agent` matches the expected sub-agent.

### 8.3 `failed`

The server could not run the request. Distinct from a refusal.

- `status.state = "failed"`.
- `status.message.parts`:
  - **DataPart** with `hr-agent/Error@v1` (primary signal):
    ```jsonc
    {
      "schema": "hr-agent/Error@v1",
      "code": "<closed v1 set>",
      "type": "<exception class name>",
      "message": "<user-safe message — no stack traces>",
      "retriable": <bool>,
      "agent_id": "<originating agent>"
    }
    ```
  - Optionally a TextPart with a user-safe message (no stack, no internal codes).
- `task.metadata.hr-agent.error` mirrors `{code, type}` and is exposed via `A2AResponse.error`.
- `execution_trace` is still emitted (may be empty).

**Closed v1 error-code set:** `auth_error`, `tool_error`, `llm_error`, `interrupt_resume_error`, `validation_error`, `internal_error`.

**Scoring tip:** EVAL-EDGE-006 ("graceful failure") requires that NO stack traces appear in `status.message`'s TextPart. Add a smoke check on the text content as part of any failure-path scenario.

### 8.4 Refusal (vs. failure)

- A refusal is a normal completion: `state="completed"`, `status.message` is plain TextPart.
- A `refusal` artifact (§6.3) lets scorers detect it without NLP.
- **This rule is load-bearing for scorers.** Branch:
  ```python
  if response.state == "failed":         # genuine failure path
      ...
  elif "refusal" in response.artifacts:  # graceful refusal
      ...
  else:                                   # normal completion
      ...
  ```

### 8.5 `canceled`

`state="canceled"`. Trace and metadata reflect whatever was captured before cancellation.

---

## 9. Streaming (`message/stream`)

The same shape, expressed as A2A SDK events:

1. Initial `Task` event (empty trace, status=`submitted`).
2. `TaskStatusUpdateEvent` with `state="working"` (one transition, **not** per step).
3. `TaskArtifactUpdateEvent` for `execution_trace` repeatedly: `append=true, last_chunk=false` per batch; `last_chunk=true` on the final batch.
4. (Optional) `TaskArtifactUpdateEvent` for named outputs (`matched_jobs`, etc.).
5. Terminal `TaskStatusUpdateEvent` with the final state and `metadata`.

A `message/send` caller receives the same final Task with all events accumulated — calling either method against the same prompt MUST yield equivalent final Tasks (sans timestamps). The eval harness uses `message/send` only in v1; streaming-parity tests are integration-test scope.

---

## 10. Forward compatibility & namespace rules

- All custom keys prefixed `hr-agent.`.
- Scorers MUST ignore unknown `metadata` keys, unknown event `type` values, and unknown artifact `name`s.
- Adding a metadata key, an event type, or an artifact name is **additive** and does NOT bump `schema_version`.
- Renaming or removing any of those, or changing the meaning of an existing field, is a **breaking** change that bumps `schema_version` (e.g. `2.0`).
- The JSON Schema fixture (`evals/schemas/a2a_response.v1.json`) is the machine-readable contract. Update this doc + the schema in the same PR.

---

## 11. Identity propagation — FA vs BFF asymmetry

Identity drives `task.metadata.hr-agent.user_id`:

- **FA (hr-agent)**: client-supplied via `message.metadata.first_name` / `display_name` / `thread_id`. `identity_source = "a2a-message-metadata"`. The eval harness controls who the agent thinks it's talking to — useful for persona-based datasets.
- **BFF (backend)**: server-derived from the JWT (`ubs_auth_gpn` claim). `identity_source = "jwt"`. The eval harness CANNOT set `user_id` — it is whoever the SSO token belongs to. Identity-aware scenarios need either a per-persona token rotation or a different framing on BFF.

This is the single dimension where FA and BFF datasets diverge. Tag identity-sensitive dataset items with `requires: ["identity_metadata"]` so the runner skips them on BFF instead of failing.

### BFF gaps in v1

The BFF currently returns only `finalText`. Until Phase 2b lands, on BFF:

- `execution_trace.events` is empty (or only contains a single `agent_message` event for the final text).
- `agent_token_totals`, `latency_ms`, `tokens`, `cost`, `models` may be missing or zero.
- `state="failed"` may not include the `Error@v1` DataPart — fall back to text scraping for now.
- All trace-aware scorers will return `None` (skip) for BFF rows. Build dashboards that don't penalise that.

---

## 12. Conformance checklist (what eval smoke tests assert)

A v1-conformant response satisfies all of:

1. ☐ `status.message.parts[0]` is a TextPart with the final assistant reply on terminal states.
2. ☐ `result.artifacts` always contains exactly one `execution_trace` artifact with `data.schema="hr-agent/Trace@v1"`.
3. ☐ Trace events use the closed v1 `type` vocabulary. `tool_call` + `tool_result` pairs share `span_id` and `tool_call_id`.
4. ☐ `task.metadata` contains every always-present key in §7.
5. ☐ `cost` omitted when any observed model is unrated; `cost_unknown_models` emitted instead.
6. ☐ `state="failed"` carries an `Error@v1` DataPart and mirrors `{code, type}` to `metadata.hr-agent.error`. No stack traces in the TextPart.
7. ☐ Refusals use `state="completed"` plus a `refusal` artifact — never `state="failed"`.
8. ☐ HITL uses `state="input_required"` with the DataPart described in §8.2 and an `interrupt` TraceEvent.

The JSON Schema fixture validates (1)–(8) automatically. Run:
```python
import json, jsonschema
schema = json.load(open("evals/schemas/a2a_response.v1.json"))
jsonschema.validate(response_json, schema)
```

---

## 13. Implementation pointers — chat-evals (consumer-side)

| Concern | File |
|---|---|
| HTTP client + structured response parsing | `evals/hr_benchmarker/a2a_client.py` (`A2AResponse`, `_parse_response`, `make_a2a_predict_fn`) |
| Multi-turn / hyperparam / MLflow row assembly | `evals/hr_benchmarker/benchmarker.py` (`HRBenchmarker._row_from_prediction`) |
| Built-in + custom + trace-aware scorers | `evals/scorers.py` |
| JSON Schema fixture | `evals/schemas/a2a_response.v1.json` |
| Datasets (single-turn + multi-turn `turns`) | `evals/datasets.py` |
| Tests | `evals/tests/test_a2a_client_v1.py`, `evals/tests/test_scorers_trace.py` |

### How to write a new trace-aware scorer

1. Decorate with `@scorer` (from `mlflow.genai.scorers`; falls back to a no-op shim when mlflow isn't installed so the scorer is unit-testable).
2. Take `expectations` and `trace` (and/or `artifacts`, `task_metadata`) as kwargs — these column names are produced by `HRBenchmarker._row_from_prediction`.
3. Return `None` to skip the row (no expectation defined). Return a float in `[0, 1]` otherwise. Never raise.
4. Add to `get_custom_scorers()` so it runs by default.
5. Add a unit test in `evals/tests/test_scorers_trace.py` using the `_trace`/`_tool_call`/`_tool_result` helpers.

Example:
```python
@scorer
def my_scorer(expectations: dict, trace: Any) -> float | None:
    expected = expectations.get("my_expectation")
    if expected is None:
        return None
    events = (trace or {}).get("events", []) or []
    # ... compute score ...
```

### How to annotate a dataset for the new metrics

In `evals/datasets.py`, an item's `expectations` may include any of these keys (all optional):

| Key | Type | Used by |
|---|---|---|
| `expected_response` | str | `Correctness` |
| `response_must_contain` | str[] | `response_completeness` |
| `expected_tool_calls` | str[] | `tool_trace_f1` |
| `expected_tool_args` | `{tool: {arg: value}}` | `tool_argument_correctness` |
| `max_steps` | int | `step_efficiency` |
| `expected_routes` | str[] | `plan_quality` |
| `allowed_tool_calls` | str[] | `plan_quality` |
| `expected_actions` | str[] | `audit_log_action_taken` |
| `expected_artifacts` | `{name: schema_id}` | `card_format_correctness` |
| `requires` | str[] | runner's capability gate (e.g. `["identity_metadata"]`) |

A scorer returning `None` means the row had no expectation for that metric, so it's skipped — datasets can mix expectations per item without breaking aggregation.

---

## 14. Examples

See `hr-agent/docs/a2a_response_v1.md` §14 for the full canonical examples (completed multi-tool, failed, input-required). The eval-side test fixture in `evals/tests/test_a2a_client_v1.py::_v1_response_fixture` is a runnable, JSON-Schema-validated version of the completed example.

---

## 15. Versioning

- `metadata.hr-agent.schema_version` is the canonical version marker (read via `A2AResponse.metadata["hr-agent.schema_version"]`).
- Future versions live alongside as `a2a_response_v2.md`, `a2a_response.v2.json`, etc. Both must work in parallel for ≥1 release.
- The AgentCard's response-extension `params.schema_version` advertises the highest version a server supports — the harness can branch on it to enable v2-only assertions.

### Deferred to v1.1 (don't write scorers for these yet)

- Per-step token attribution (only per-agent subtotals + task total in v1).
- Per-step `started_at` / `ended_at` (only task `latency_ms` in v1).
- Per-step principal / delegated identity (only task-level `user_id` in v1).
- Streaming/chunked `tool_call` args (atomic in v1).
- Cross-task trace correlation (`trace_id` is task-scoped in v1).
- Currency other than USD.

---

## 16. Cross-references

- Server-side spec (single source of truth): `hr-agent/docs/a2a_response_v1.md`
- Plan: `~/.claude/plans/lets-first-work-on-staged-lemur.md`
- JSON Schema: `evals/schemas/a2a_response.v1.json`
- Eval-metrics design: `evals/eval_metrics.md`, `evals/mlflow_metrics_feasibility.md`
- A2A protocol: https://a2a-protocol.org/
