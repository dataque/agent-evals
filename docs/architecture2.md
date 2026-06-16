# Agent Evaluation Harness — Architecture

**Status:** Proposed
**Source of truth:** [`docs/metrics.md`](./metrics.md) (the 26-metric prod ranking). Everything else — the
reference PoC in `chat-evals-for-reference/`, the prior `docs/implementation_plan.md`, this document — is a
pointer. The authoritative description of system behaviour is the live code in `backend` and `frontend`.

**Scope:** A framework-independent evaluation harness for the production HR Agent — `frontend`
(React/Next.js + `@ag-ui/client` + `@assistant-ui/react` over SSE) talking to `backend` (Spring Boot 3 +
Spring AI, orchestrator + three local subagents on Azure OpenAI `gpt-5.2`, persisted in Cosmos DB).
First implementation is **Python + MLflow**, but MLflow is a swappable backend, not the spine.

---

## 0. The finding that reshapes the plan

The earlier `docs/implementation_plan.md` proposes **"A2A first, AG-UI later"** on the premise that the
backend exposes both an ag-ui SSE endpoint and an A2A JSON-RPC endpoint at `/api/v1/bff/ai/agent/a2a`.
**That premise is false.** Ground truth from the code:

- The **only** HTTP route on the BFF is `POST /api/v1/bff/ai/agent/sse`, which returns an `SseEmitter` —
  the ag-ui event stream. See `controller/ai/AgentController.java:26-32`.
- `ai/orchestrator/A2aOrchestratorAdapter.java` exists and uses the `io.a2a.server` SDK, but it is **not
  wired to any controller/route** — there is no `/a2a` endpoint.
- As written, that adapter returns only `result.finalText()` (`A2aOrchestratorAdapter.java:59`) — a single
  string. It **discards the tool trace, tool results, steps, and reasoning** that most Tier-1 metrics need.
  `TransportType` (`AG_UI`, `A2A`) is an *internal* notion of how the shared `Orchestrator` core was invoked,
  not a second public surface.

Consequence: the sequencing inverts. **AG-UI / SSE is the only viable interface today and also the richest**
— the backend emits 30 ag-ui event types including `TOOL_CALL_START/ARGS/END/RESULT`, `STEP_*`,
`REASONING_*`, and the `RUN_*` lifecycle (`agui/server/EventFactory.java`). A2A is a *future* clean-up that
would require a backend CR both to expose a route and to enrich it to carry a trace.

---

## 1. The BFF interface

The harness must call the agent under test through exactly one seam, with a normalized return shape, so that
scorers never see the transport. Three candidate interfaces were considered:

| Option | Available today? | Carries the tool-trace / events metrics need? | Effort | Verdict |
|---|---|---|---|---|
| **A. Reuse SSE + AG-UI** — drive `/api/v1/bff/ai/agent/sse` from Python and reconstruct the trace from events | Yes (only endpoint) | Yes — full 30-event stream | Low | **Primary — adopt now** |
| **B. Dedicated A2A endpoint** | No — adapter exists but unwired; returns only `finalText()` | No, as written | High (BE CR: expose route **and** enrich to emit a trace) | Future "clean" option |
| **C. Mix** | — | — | — | **The evolution path**: SSE now; add A2A later behind the same client seam |

**Decision: Option A now, behind a transport-agnostic `AgentClient` seam** so Option B can slot in later
without touching a single scorer. Two sub-decisions follow:

1. **Drive the SSE endpoint directly from Python — do not route eval through the frontend.**
   `metrics.md` §10.1 suggests wrapping the FE's `@ag-ui/client` `HttpAgent`. That couples the harness to
   Node + assistant-ui and is hard to make deterministic. A Python `httpx` SSE client hitting `/sse` gives
   full event access, language independence, and clean replay. The FE's
   `runtime/createAgentWithEventInterception.ts` tap (today it listens only for `TOOL_CALL_RESULT`) is
   repurposed for an *optional, later* job — **shadow-capturing real user sessions** into the replay corpus —
   not for driving scenarios.
2. **Live runs need synthetic JWTs.** `/sse` requires an Entra ID Bearer token and reads `ubs_auth_gpn` for
   the user id (`AgentController.java:42`). Live eval therefore depends on a sandbox-tenant / pre-signed
   test-JWT capability (metrics.md CR #7). **Replay runs need no auth**, which is why record/replay is a
   Phase-0 primitive, not an afterthought.

---

## 2. Design principles — how framework independence is achieved

The reference PoC got halfway: its scorers were pure functions, but the runner called
`mlflow.genai.evaluate()` directly and every scorer imported MLflow's `@scorer` decorator. We invert that
coupling with **four ports**. MLflow then becomes one set of adapters behind them.

1. **`AgentClient` seam** — decouples scorers from transport (SSE today, A2A later, replay always).
2. **Normalized `Trace`** — one trace model reconstructed from ag-ui events, shaped like the PoC's
   `execution_trace` artifact so the proven deterministic scorers port almost verbatim.
3. **`Tracker` port** — `MlflowTracker` (maps our scorers → `mlflow.genai.evaluate`, logs runs/params/metrics)
   **or** `JsonTracker` (writes the report to disk, zero framework). Porting to another platform
   (LangSmith / Phoenix / Braintrust) means writing one new Tracker; scorers, datasets, clients, and the
   trace model are untouched. **This is the operational test of framework independence.**
4. **`Judge` port** — "an LLM judge" is abstracted from any one library; adapters wrap MLflow-native scorers,
   DeepEval, and Ragas. This honours metrics.md's "judge family ≠ agent family" rule (§10.5) and lets us swap
   judges per metric.

Our own `core/` types are the spine. Everything proprietary points *inward* to them; nothing in `core/`,
`scorers/`, or `datasets/` imports a framework. The concrete improvement over the PoC: scorer modules are
decorator-free pure functions over our types, and the `MlflowTracker` wraps them into MLflow scorers only at
the boundary — removing the MLflow import from scorer code entirely.

---

## 3. High-level components

```
core/        framework-free domain: events, Trace, AgentRun, Scenario/Expectations,
             Scorer protocol + ScorerSpec(tier/lane/judge?/cr/veto), EvalReport
clients/     THE BFF SEAM. AgentClient ABC → agui_sse/ (driver, parser, trace
             reconstructor, stream-health), a2a/ (future stub), replay/ (record+play)
judges/      Judge port + mlflow_native / deepeval / ragas adapters
scorers/     one pure-fn module per metric family (trace, schema, quality,
             compliance, operational, project-specific)
schemas/     bring-our-own JSON Schema per tool result (the #4 schema-drift source)
tracking/    Tracker port + mlflow_tracker / json_tracker
datasets/    scenario loaders + synthetic identities (eval-user-A/B for #8)
runner/cli   selects client × dataset × scorer-set(tier/lane) × mode(live/replay) × identity
```

The seams that matter most, sketched:

```python
class AgentClient(ABC):                       # clients/base.py — the BFF seam
    def run(self, turn_input: str, *, identity: Identity,
            thread: ThreadRef) -> AgentRun: ...

# AgentRun = final_text + Trace + raw_events + Timing(TTFT, total) + usage? + state

Scorer = Callable[[AgentRun, Expectations, Context], "ScoreResult | None"]  # None = skip
# pure function over core types — NO framework import in scorer modules
```

**`TraceReconstructor`** (in `clients/agui_sse/reconstruct.py`) is the linchpin new component. It folds the
ordered ag-ui event stream into the normalized `Trace`:

- assembles streamed `TOOL_CALL_ARGS` deltas into final JSON **only after** `TOOL_CALL_END` (mid-stream JSON is
  malformed — metrics.md §3 #3);
- maps subagent routing through the `Task` tool to a `route` event, so trace scorers can assert *which
  subagent's tool fired*, not just "a tool fired" (metrics.md §8.10);
- stamps latency markers — `RUN_STARTED` → first `TEXT_MESSAGE_CONTENT` = TTFT (metric #13);
- runs protocol-invariant checks for stream health (metric #24) in `streamhealth.py`.

---

## 4. Folder structure

```
agent-evals/
├── pyproject.toml • README.md • .env.example
├── config/
│   ├── targets.yaml        # env → base_url, auth mode, transport(agui_sse|a2a)
│   ├── judges.yaml         # judge registry (provider/model/role) — judge ≠ agent
│   └── lanes.yaml          # scorer sets + thresholds per lane (quality | compliance)
├── docs/
│   ├── metrics.md          # ← SOURCE OF TRUTH (exists)
│   ├── architecture2.md    # this document
│   └── adr/
│       ├── 0001-bff-interface-agui-sse.md
│       ├── 0002-framework-independence-ports.md
│       └── 0003-trace-reconstruction-model.md
├── agent_evals/
│   ├── core/               events.py trace.py run.py scenario.py score.py registry.py report.py
│   ├── clients/
│   │   ├── base.py
│   │   ├── agui_sse/       client.py parser.py reconstruct.py streamhealth.py
│   │   ├── a2a/            client.py            # future; blocked on BE CR (ADR-0001)
│   │   └── replay/         recorder.py player.py
│   ├── judges/             base.py mlflow_native.py deepeval.py ragas.py
│   ├── scorers/
│   │   ├── trace_metrics.py      # #2 #3 #16 #18 #19
│   │   ├── schema_adherence.py   # #4  (uses schemas/)
│   │   ├── quality.py            # #1 #5 #6 #10 #11 #12 #17 #20 #21
│   │   ├── compliance.py         # #7 #8 #9 #15  (veto lane)
│   │   ├── operational.py        # #13 #14 #22 #23 #24
│   │   └── project/followup_scenario.py   # docs/metrics/followup-scenario-correctness.md
│   ├── schemas/tools/      suggest_requisitions.json draft_message.json analyze_talent_profile.json …
│   ├── tracking/           base.py mlflow_tracker.py json_tracker.py
│   ├── datasets/           loader.py identities.py
│   ├── runner.py • cli.py
├── data/
│   ├── golden/             # scripted scenarios w/ sparse expectations (never expires)
│   ├── compliance/         # veto-lane scenarios (#7 #8 #9 #15)
│   ├── bias/               # paired adversarial-persona prompts (#15)
│   └── replay/             # recorded (redacted) event streams
└── tests/
    ├── test_reconstruct.py test_streamhealth.py test_scorers_*.py
    └── fixtures/           # canned ag-ui event streams
```

---

## 5. Data model

### 5.1 Normalized event and trace

The 30 backend event types collapse into a small set of trace event kinds the scorers care about, keeping the
PoC's vocabulary so its scorers carry over:

```
TraceEvent.kind ∈ { route, reasoning, tool_call, tool_result, step, agent_message }
```

| Trace concept | Reconstructed from ag-ui events |
|---|---|
| `tool_call{ id, tool, args }` | `TOOL_CALL_START` + accumulated `TOOL_CALL_ARGS` + `TOOL_CALL_END` |
| `tool_result{ id, tool, status, result }` | `TOOL_CALL_RESULT` |
| `route{ to_subagent, reason }` | `Task` tool call → `EnumConstrainedTaskTool` `subagent_type` |
| `reasoning{ text }` | `REASONING_MESSAGE_*` / `THINKING_*` |
| `step{ name }` | `STEP_STARTED` / `STEP_FINISHED` |
| `agent_message{ text }` | `TEXT_MESSAGE_START/CONTENT/END` |
| `Timing{ ttft, total, aborted }` | `RUN_STARTED` → first `TEXT_MESSAGE_CONTENT` → `RUN_FINISHED`/close |

### 5.2 Scenario and sparse expectations

The PoC's sparse-expectations contract is kept: each scorer reads its own `expectations.*` field and returns
`None` (skip) when the field is absent — so one row drives many scorers and adding a metric needs no dataset
rewrite.

```yaml
- id: skills_then_match
  identity: eval-user-A            # which synthetic JWT (live runs)
  turns:
    - input: "What skills do you see in my profile?"
      expectations:
        expected_tool_calls: [analyze_talent_profile, suggest_skills, emit_followups]
        expected_routes: [talent-profile-management-agent]
        expected_schema: { suggest_skills: top_and_additional_skills }
        followup_scenario_id: skills_review_pending     # project scorer
    - input: "Save these and find me matching roles"
      expectations:
        expected_tool_args:
          save_skills: { version: "$from:get_talent_profile.version" }  # optimistic-lock chain (#3)
        expected_tool_calls: [save_skills, suggest_requisitions]
        expected_response: "..."                          # golden, for #6
```

---

## 6. How the 26 metrics land

Representative mapping; the rest follow the same pattern. "CR" columns reference `metrics.md` §9.

| Metric | Component | Inputs | CR-blocked? |
|---|---|---|---|
| #2 Tool Trace F1, #3 Tool Arg (+ `save_skills` version chain), #18 Step Eff, #19 Plan | `scorers/trace_metrics.py` over reconstructed `Trace` | trace + golden | No (FE tap optional) |
| #4 Schema Adherence | `scorers/schema_adherence.py` + `schemas/tools/*.json` | tool results + our schemas | Schemas hand-authored now; codegen = CR #4 |
| #24 Stream Health, #13 Latency/TTFT | `clients/agui_sse/streamhealth.py` + `reconstruct.py` | raw events + timing | No (full server-side latency = BE CR #9) |
| #1 TSR, #5 Faithfulness, #6 Equivalence, #10 Completeness, #12 Topic, #21 Role, #17 Rubric, #20 Relevancy | `scorers/quality.py` via `Judge` port | final state / tool-output context / golden | No |
| #11 Knowledge Retention | `scorers/quality.py` — deterministic Cosmos-read variant + judge cross-check | persisted message history | No |
| #7 Safety, #8 Cross-User Isolation, #9 Refusal, #15 Bias | `scorers/compliance.py` (veto lane) | #8 = dual-JWT replay via `datasets/identities.py` | #8 partition audit = CR #2; #9 clean = CR #8 |
| #14 Token/Cost, #16 Audit/Action-Taken, #23 User Feedback | `scorers/operational.py` | usage / audit container / thumbs | **#14 = CR #3, #16 = CR #6, #23 = CR #5** |
| #22 String/Must-contain | `scorers/operational.py` | substrings | No |
| followups scenario correctness | `scorers/project/followup_scenario.py` | `emit_followups.scenario_id` vs upstream tool's `requiredNextAction` | No (deterministic) |
| #25–#29 RAG-flavoured | not implemented — no retriever in `backend` | — | N/A |

The followups scorer is a strong deterministic project signal: each tool output carries a `requiredNextAction`
directive, and `emit_followups` must echo the matching `scenario_id` (from a fixed enum) — so correctness is a
direct comparison, no judge required.

---

## 7. Run modes and lanes

- **Live runs** hit a real `gpt-5.2` via `/sse`: cost real tokens, vary across runs, require Cosmos isolation
  and synthetic JWTs. Use for nightly regression and pre-release gates.
- **Replay runs** re-score recorded event streams: free, deterministic, judge-only. Use for scorer iteration,
  dataset triage, and comparing judge prompts on a frozen agent. **Both are built from Phase 0**, via the
  `replay/` recorder+player behind the same `AgentClient` seam.
- **Quality lane** vs **Compliance lane**: veto metrics (#7 Safety, #8 Isolation, #9 Refusal, #15 Bias) run on
  every release candidate against a fixed compliance dataset with stricter thresholds and required human
  sign-off, in a **separate, non-bypassable CI workflow** — structural, not advisory (`lanes.yaml`).

### Eval data lifecycle

1. **Golden datasets** — scripted scenarios with expectations, versioned in `data/golden/`, never expire.
2. **Replay corpus** — redacted real sessions in `data/replay/` (separate store, no Cosmos TTL,
   access-controlled). Do not depend on prod Cosmos for ground truth older than ~25 days (30-day TTL).
3. **Live shadow traffic** — production requests cloned to an eval lane via `X-Eval-Mode: shadow`
   (CR #10), sampling-driven, for drift detection.

---

## 8. Phased implementation plan

- **Phase 0 — Foundations (no CR; unblocks everything).** `core/` types; the `AgentClient` seam; the
  `agui_sse` client + parser + **TraceReconstructor**; `replay/` record+play; `JsonTracker` **and**
  `MlflowTracker`; dataset/identity loaders; CLI. *Exit criterion:* drive one scenario live against `/sse`,
  reconstruct a trace, replay it offline, and emit a report through both trackers.
- **Phase 1 — Deterministic metrics (cheap, high-value, judge-free).** #2, #3 (incl. version chain), #4
  (schema adherence), #18, #22, #24, #13-TTFT, followups scenario. Run in CI for ~$0; proves the trace model.
- **Phase 2 — Judge metrics.** `Judge` port + DeepEval / Ragas adapters; #1, #5, #6, #10, #11, #12, #17, #19,
  #20, #21. Enforce judge-family ≠ agent-family with a periodic human-judge sanity sample.
- **Phase 3 — Compliance lane (non-bypassable CI).** #7, #8 (dual-JWT replay), #9, #15 (paired-prompt
  dataset); stricter thresholds + required human sign-off; failure blocks release.
- **Phase 4 — CR-blocked operational + feedback.** #14, #16, #23, the partition-key isolation audit, and the
  `X-Eval-Mode` shadow-traffic lane — each gated on its backend/frontend CR (#2, #3, #5, #6, #10).

---

## 9. Risks and decisions to confirm

1. **Capture mechanism** — recommendation is to drive `/sse` directly from Python; the alternative (route
   through the FE `HttpAgent` tap) couples the harness to Node and is harder to make deterministic.
2. **Synthetic-JWT availability** — live runs are blocked without it. Phases 0–2 still proceed entirely on
   replay + recorded fixtures; only live nightly runs wait on CR #7.
3. **Tracking** — MLflow first via `MlflowTracker`, with `JsonTracker` always present as the framework-free
   fallback (this is what proves portability). Keep both from day one.
4. **Schema drift (#4)** — schemas are hand-authored now (FE TS generics in `tools/*.tsx` vs BE Java records);
   without codegen (CR #4) they will drift again. The scorer catches *structural* failures only, not content.
5. **Judge-on-self bias** — the agent is on `gpt-5.2`; the judge must be a different family or version, with a
   periodic human-judge sanity sample to estimate judge bias.
6. **Infra vs quality failures** — transient Cosmos write failures must be tagged and retried-once-then-skipped,
   not scored as agent-quality failures.

---

## 10. Relationship to existing artifacts

- **`docs/metrics.md`** — the source of truth; this document is its implementation architecture.
- **`docs/implementation_plan.md`** — superseded on the central point: its "A2A-first" sequencing rests on a
  non-existent `/a2a` endpoint (see §0). Recommend annotating or replacing it.
- **`chat-evals-for-reference/`** — reusable: the sparse-expectations dataset contract, the pure-function
  scorer style, the trace-event vocabulary, and the predict-function-as-seam idea. Not reusable: the A2A
  JSON-RPC transport, the single named `execution_trace` artifact, and the single-response (non-streaming)
  assumption — all replaced here by SSE event capture + trace reconstruction.
