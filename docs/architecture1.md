# Agent-Evals — A First-Principles Design

Status: design proposal · Scope: an evaluation system that implements the 26
metrics in [`metrics.md`](./metrics.md) for the production HR Agent
(`frontend` over ag-ui/SSE → `backend` orchestrator + three subagents on Azure
OpenAI, persisted in Cosmos DB).

This document designs the eval **from the metrics up**. The chat-evals PoC and
the earlier `implementation_plan.md` are treated as *reference points*, not as
the skeleton. Where the PoC starts from a protocol (A2A) and bolts metrics on,
this design starts from "what must a system be able to observe, do, and judge to
compute all 26 metrics" and lets the architecture fall out of that.

---

## 0. Verified ground truth (what the system is being built against)

- The **only live transport** is ag-ui over SSE: `POST /api/v1/bff/ai/agent/sse`
  (`AgentController.java:26-32`). It emits 28 event types
  (`TEXT_MESSAGE_*`, `TOOL_CALL_START/ARGS/END/RESULT`, `STEP_*`,
  `REASONING_*`/`THINKING_*`, `STATE_DELTA`, `MESSAGES_SNAPSHOT`,
  `RUN_STARTED/FINISHED/ERROR`).
- A2A exists **internally but is not HTTP-exposed**: `A2aOrchestratorAdapter.java`
  + `TransportType.A2A` + the `io.github.a2asdk:a2a-java-sdk-server-common`
  dependency are present, but no controller wires A2A to a route, and the A2A
  path returns only `result.finalText()` with the ag-ui event subscribers
  **suppressed** (`OrchestratorImpl.java:134` wires emitters only for
  `TransportType.AG_UI`). A2A today yields a bare final string — no tool trace,
  args, steps, card payloads, or tokens.
- No token/cost extraction, no audit log, no safety filter, no per-request
  correlation id (`metrics.md` §8).
- Cosmos `message`/`thread` containers, hierarchical partition
  `/userId/threadId/agentId`, 30-day TTL.

**Consequence:** the ag-ui SSE stream is the single richest source of agent
execution in the system, and it needs **zero backend change** to consume. It is
the spine of this design. A dedicated A2A endpoint is explicitly *not* on the
critical path.

---

## 1. Method: derive the system from the metrics

For each metric, ask what observation it requires; the architecture is the
minimal machine that can produce and score those observations. Decomposing
`metrics.md` by required input, every metric needs one or more of seven
capabilities:

| Capability the system must have | Metrics (`metrics.md` #) | Source of input | Judge? | Backend CR? |
|---|---|---|---|---|
| **A. Final response** (last assistant text / end-state) | 1, 6, 7, 9, 12, 17, 20, 21, 22 | `TEXT_MESSAGE_*` reconstruction / persisted assistant msg | mostly | — |
| **B. Tool trace** (ordered calls, names, routing) | 2, 16 (weak), 18, 19 | `TOOL_CALL_START/END`, `Task`-tool args (subagent routing), `STEP_*` | no | — |
| **C. Tool arguments** (assembled JSON, incl. `save_skills` version chain) | 3 | `TOOL_CALL_ARGS` deltas → assemble at `END` | no | — |
| **D. Tool result payloads** (structured) | 4 (schema), 5 (grounding) | `TOOL_CALL_RESULT.content` | 5 only | — (#4 wants schema codegen) |
| **E. Multi-turn transcript** | 1 (M), 10, 11 | shared thread across turns / Cosmos `MessageEntity` | yes | — |
| **F. Persisted-state probe** (Cosmos writes, partition keys, audit) | 8, 11 (deterministic), 16 (strong) | data-access reads in eval-mode | no | partition-log / audit-container |
| **G. Identity & timing context** | 8 (dual-user), 13 (TTFT/abort), 24 (ordering) | two JWTs + event timestamps/sequence | no | sandbox JWTs (#8) |
| **— Operational / out-of-band** | 14 (token/cost), 23 (prod feedback) | Spring AI `usage` (not extracted) / thumbs UX | no | usage-extract (#14), thumbs UX (#23) |
| **— N/A** | 25–29 (RAG) | no retriever in backend | — | — |

Two conclusions fall straight out:

1. **The expensive, load-bearing capability is capture (A–D + G-timing).** ~10
   metrics are functions of the agent's *execution*, not just its answer. The
   richest source of that execution is the ag-ui SSE event stream, which is also
   the only live transport. So capture is the spine, and ag-ui is the default
   protocol. (This is the inversion from the reference design.)
2. **Every metric is a pure function of one captured object plus its
   expectations.** Nothing needs the live agent at scoring time. The canonical
   primitive is therefore not "a response" — it is a complete, replayable
   **Episode**.

---

## 2. The core primitive: the Episode

> An **Episode** is a complete, transport-independent, serializable observation
> of one agent execution (one turn, or one multi-turn conversation), containing
> everything any metric could read.

```
Episode
  id, scenario_id, family, turn_index, thread_id
  identity            # which synthetic user drove it (for isolation)
  request             { messages[], context }
  response
    final_text
    messages[]        # assistant / tool / reasoning, ordered
  trace
    events[]          # canonical: run_started | text_delta | tool_call | tool_args
                      #   | tool_result | step_started/finished | reasoning | route
                      #   | state_delta | snapshot | run_finished | run_error
    tool_calls[]      { name, args(assembled), result(payload), status, subagent, t_start, t_end }
    routes[]          { from: orchestrator, to: subagent }   # from the Task tool
    reasoning[]
  timings             { t_request, t_first_token, t_final_token, t_run_finished, aborted }
  usage               { tokens, cost } | null   # null until backend CR
  state_probe         { writes[], reads[], partition_keys[] } | null  # null until eval-mode hooks
  retrieved_context[] | null   # reserved extension point for Tier-3 RAG metrics
  raw                 # original wire bytes, for replay/debug
```

Why this is the right primitive, derived from the spec's own constraints:

- **Decouples capture from scoring.** Capture is expensive (real `gpt-5.2`
  tokens, Cosmos isolation, run-to-run variance); scoring should be cheap and
  repeatable. Make the Episode the hand-off and **replay is free** — re-score a
  frozen Episode to iterate scorers, A/B two judge prompts, or run the
  cross-judge bias study `metrics.md` §10.5 asks for, at zero agent cost. The
  PoC couples `predict → score` and can do none of this.
- **Forced by Cosmos's 30-day TTL** (`metrics.md` §8.6). Ground truth ages out;
  the eval needs durable, reproducible inputs. Persisting Episodes to an
  eval-owned store *is* the reproducibility layer the spec demands — not an
  optimization.
- **Collapses the protocol question.** A2A, ag-ui, and (later) replay are three
  **producers** of the same Episode. Scorers never see a wire format.

The system then has one rule:

```
metric(Episode, Expectations, Judge?) -> Score
```

Everything else exists to **produce**, **persist**, **judge over**, and
**aggregate** Episodes.

---

## 3. Reference architecture

Three concentric rings, two swap-seams, two run modes.

```
                            ┌──────────────────────────────────────────────┐
   RING 3 — bindings        │  MLflowRunner · RunTracker · CI gates · CLI   │   ← FRAMEWORK SEAM
   (swappable infra)        └──────────────────────────────────────────────┘
                            ┌──────────────────────────────────────────────┐
   RING 2 — capabilities    │ Driver(ProtocolAdapter) · Reconstructor ·     │   ← PROTOCOL SEAM
   (services that produce/  │ JudgeClient · StateProbe · IdentityManager ·  │
    consume Episodes)       │ DatasetStore · EpisodeStore · Aggregator/Gate │
                            └──────────────────────────────────────────────┘
                            ┌──────────────────────────────────────────────┐
   RING 1 — domain core     │  Episode · Expectations · Scenario · Score ·  │   pure python,
   (pure, framework-free)   │  Metric registry  (the 26 as pure functions)  │   no mlflow, no http
                            └──────────────────────────────────────────────┘
```

**The pipeline (one eval run):**

```
Scenarios ─► Planner ─► Driver ─► Capture ─► Reconstructor ─► [StateProbe] ─► Metric engine ─► Aggregator ─► Gate ─► Reporter
            (expand:     (drive    (raw       (→ canonical     (attach        (pure fns over    (per-metric   (veto    (MLflow
             turns,       agent     event      Episode)         Cosmos          Episode)          per-family)   lane vs   runs +
             bias-pairs,  per id    stream)                     facts)                                          quality)  baseline
             dual-id)     & turn)                                                                                          diff)
```

**Two run modes**, both first-class because the metrics force them:

- **Live** — Planner→…→Reporter against the real agent. Captures + persists
  Episodes. Nightly / pre-release. Needs Cosmos isolation + eval JWTs.
- **Replay** — start at the Episode store, re-run Metric engine→Reporter only.
  Free, deterministic, judge-only. Scorer dev, dataset triage, judge
  calibration. This is what Episode-persistence buys you.

---

## 4. Components (each is a capability from §1)

| Ring | Component | Responsibility | First-principles origin |
|---|---|---|---|
| 1 | **Episode / Expectations / Scenario / Score** | Typed domain models (pydantic) | The eval is a compliance gate at a bank — its own correctness matters, so inputs are typed, not dicts |
| 1 | **Metric registry** | The 26 as pure `(Episode, Expectations, Judge?) -> Score`; each *declares* the Episode/Expectation fields it reads and **self-skips** when absent | "Every metric is a pure fn over an Episode" (§2) |
| 2 | **ProtocolAdapter** (`agui`, `a2a`, `replay`) | Drive the agent over one transport; hand raw stream to capture | Protocol seam (§2) |
| 2 | **Reconstructor** | ag-ui events → canonical Episode (assemble arg deltas at `TOOL_CALL_END`, map `Task` calls → routes, derive TTFT/abort from `RUN_*`) | Cap A–D, G-timing |
| 2 | **JudgeClient** | Controlled LLM-judge: **pinned model (≠ agent family), versioned rubric library, structured Score output, human-calibration sampling** | `metrics.md` §10.5 (judge-bias) makes the judge a *subsystem*, not a model string |
| 2 | **StateProbe** | Read Cosmos in eval-mode: partition keys, writes, audit rows | Cap F (#8, #11-det, #16) |
| 2 | **IdentityManager** | Synthetic users (A/B), JWT acquisition, thread minting, multi-turn threading | Cap E + G (#8, #10, #11) |
| 2 | **DatasetStore / Planner** | Scenario *families*; expand multi-turn, **bias-paired**, **dual-identity** variants | #15 needs paired prompts; #8 needs dual-run — datasets can't be flat |
| 2 | **EpisodeStore** | Durable, redacted persistence of captured Episodes (eval-owned, no TTL) | §8.6 (Cosmos TTL) + replay |
| 2 | **Aggregator + Gate** | Roll up per-metric/per-family; **separate compliance veto lane** (#7, #8, #9, #15) with stricter thresholds + required sign-off | §8.11 / §10.4 — gating is *structural* |
| 3 | **Runner / RunTracker** | Bind to `mlflow.genai.evaluate` + experiment tracking; one impl of a thin port | Framework seam |
| 3 | **CLI / CI workflows** | `--project --protocol --target --mode(live/replay) --scorers --identity`; nightly + compliance gates | Operability |
| — | **FeedbackIngest** (out-of-band) | Thumbs-down sampling → redaction → regression dataset | #23 is a *pipeline*, not a per-row scorer |

---

## 5. Data model — typed scenarios & layered expectations

Expectations are **layered by metric consumer**, so authoring a scenario is
declaring only what you want checked:

```python
Scenario
  id, family            # skill_review | requisition_match | outreach | refusal
                        #   | injection | bias_pair | isolation
  input: SingleTurn | MultiTurn
  identities: [A] | [A, B]          # dual ⇒ isolation run
  variant_of: scenario_id | None    # bias pairs link here (differ only by protected attr)
  expectations:
    answer:     { expected_response?, must_contain?, must_not_contain? }     # 6, 22
    tools:      { expected_calls?, expected_args?, allowed_calls?,           # 2, 3, 18, 19
                  expected_routes?, max_steps? }
    schema:     { tool -> json_schema_ref }                                  # 4
    actions:    { expected_mutations? }                                      # 16
    safety:     { pii_forbidden?, refusal_expected?, redirect_rubric? }      # 7, 9
    grounding:  { faithful_to: tool_outputs }                                # 5
    rubrics:    { name -> rubric_text }                                      # 12, 17, 21
    isolation:  { user_b_data_must_not_appear }                              # 8
```

This is the first-principles upgrade over the PoC's untyped `expectations` dict:
**each metric binds to a named expectation slice**, the type system tells you
which metrics a scenario can drive, and a metric with no matching slice cleanly
returns `skip` (not `0`).

---

## 6. Coverage closure (the design ⇄ the spec)

Proof the machine computes the whole spec, by capability:

- **Deterministic, trace-only** (no judge, CI-safe, run on every commit): #2, #3,
  #4, #8, #13, #16 (weak), #18, #22, #24 → Metric engine over Episode, no
  `JudgeClient`.
- **Judge-backed** (JudgeClient, pinned non-`gpt-5.2` model): #1, #5, #6, #7, #9,
  #10, #11, #12, #15, #17, #19, #20, #21.
- **State-probe** (Cosmos eval-mode): #8 (partition audit), #11 (deterministic
  retention), #16 (strong, post audit-container CR).
- **Operational / out-of-band**: #13 (client-side timing, no CR), #14 (usage CR
  or tokenizer estimate), #23 (FeedbackIngest pipeline).
- **N/A, with an extension point reserved**: #25–29 — the `retrieved_context[]`
  hook on the Episode lets them light up *if* a retriever is added, but nothing
  is built now.

No metric in `metrics.md` falls outside a capability; nothing in the capability
set is unused.

---

## 7. Folder structure (capability-organized, Python-first)

```
agent-evals/
├── pyproject.toml                      # mlflow[databricks], httpx, pydantic, jsonschema,
│                                       #   msal, pyyaml, python-dotenv; [extra] deepeval
├── docs/  { metrics.md, architecture1.md, adr/00x-*.md }
├── agent_evals/
│   ├── domain/                         # RING 1 — pure, no mlflow/http
│   │   ├── episode.py                  #   the core primitive
│   │   ├── expectations.py  scenario.py  score.py
│   │   └── metrics/                     # the 26 as pure functions, grouped by capability
│   │       ├── trace.py     # 2,3,18,19     schema.py # 4
│   │       ├── answer.py    # 6,20,22       safety.py # 7,9   grounding.py # 5
│   │       ├── conversation.py # 10,11      rubric.py # 12,17,21
│   │       ├── isolation.py # 8             operational.py # 13,16,24
│   │       └── registry.py              # name → metric + declared inputs
│   ├── capture/                        # RING 2 — produce Episodes
│   │   ├── protocols/ { agui/, a2a/, replay/ }
│   │   ├── reconstruct.py              # events → Episode  (the main new build)
│   │   └── episode_store.py
│   ├── judge/        { client.py, rubrics/, calibration.py }
│   ├── state/        cosmos_probe.py
│   ├── identity/     { jwt.py, threads.py, planner.py }
│   ├── pipeline/     { aggregate.py, gate.py, report.py }
│   ├── runners/      { base.py, mlflow_runner.py }    # RING 3 — framework seam
│   └── cli/__main__.py
├── projects/                           # config + data, not framework
│   ├── hr_agent_poc/  { targets.yaml, datasets.py, scorers.py }   # A2A regression anchor
│   └── backend/       { targets.yaml, datasets.py, rubrics.py, schemas/*.json }
├── tests/  { test_reconstruct.py, test_metrics_*.py, fixtures/episodes/ }
└── .github/workflows/ { ci.yml, nightly_eval.yml, compliance.yml }
```

---

## 8. Build order (capabilities in dependency order)

The dependency graph is `domain → capture → {judge, state, identity} → gate`.

- **P0 — Domain core + replay correctness.** Episode/Expectations/Score/registry;
  the deterministic metrics; an A2A adapter + `hr_agent_poc` to **reproduce the
  chat-evals baseline** (numbers already trusted) — validates the engine before
  it touches the real backend.
- **P1 — ag-ui capture (the spine).** SSE client + Reconstructor + EpisodeStore.
  Lights up the deterministic trace-only tier (#2, 3, 18, 22, 24) + Latency/TTFT
  (#13) on real backend traffic, **zero CR**.
- **P2 — Judge subsystem.** `JudgeClient` (pinned judge), rubric library,
  calibration sampling. Lights up #1, 5, 6, 7, 9, 12, 17, 19, 20, 21 and the
  multi-turn pair #10, 11.
- **P3 — Schema adherence + grounding.** `schemas/*.json` authored from the
  `frontend` TS `Result` types / `backend` Java records (#4); Faithfulness over
  tool-result context (#5).
- **P4 — Compliance lane + isolation.** StateProbe + IdentityManager dual-JWT
  (#8); Bias paired datasets (#15); the veto Gate wired as a non-bypassable CI
  workflow.
- **P5 — Operational & ingestion.** Token/Cost (#14, behind the usage CR;
  estimate until then); FeedbackIngest (#23).

**Backend CRs**, tracked but **off the P0–P3 critical path**: usage+cost
extraction (#14), eval-mode partition logging (#8), audit container (#16-strong),
shared schema codegen (#4-hard), `X-Eval-Mode` side-effect suppression, sandbox
eval JWTs. A dedicated A2A HTTP endpoint is explicitly *not* required.

---

## 9. Independence — kept honest, not nominal

MLflow does three separable jobs here; the design ports each behind a seam so
"framework-independent" is real, not a label:

1. **Aggregation loop** → our thin `Runner`; `MLflowRunner` is one impl over
   `mlflow.genai.evaluate`.
2. **Experiment tracking** → a `RunTracker` port.
3. **Builtin judges** (Correctness / Safety / Guidelines) → **we own the metric
   definitions** (rubric text, judge model, Score contract) in
   `domain/metrics/`; MLflow's scorer is merely one `JudgeClient` *backend*. This
   is the deliberate first-principles call — if half the metrics *are*
   `mlflow.genai.scorers.*` objects, the system is not portable. Porting to
   another stack = new `Runner` + new `JudgeClient` backend; the Episode model,
   the Reconstructor, and all 26 metric functions don't move.

Protocol independence comes free from the same Episode seam (A2A ↔ ag-ui ↔
replay).

---

## 10. Decisions to lock before building

1. **Episode-centric capture/replay split** as the core architecture (vs the
   PoC's coupled predict→score). *Recommend: yes — it enables replay,
   judge-calibration, and TTL-durability.*
2. **Own the judged-metric definitions** (treat MLflow scorers as a backend) vs
   accept MLflow lock-in for builtins to move faster. *Recommend: own them — the
   difference between real and nominal portability, and the spec wants both judge
   control (§10.5) and framework portability.*
3. **Judge model** — confirm a non-`gpt-5.2` judge is available (a different
   family, or `gpt-4o`) so self-bias is controllable from day one.
4. **EpisodeStore substrate** — eval-owned Cosmos container vs object storage
   (Parquet/JSONL) for the durable, redacted Episode corpus.

---

## Appendix — relationship to the reference design

| Reference (`implementation_plan.md`, chat-evals) | This design |
|---|---|
| A2A is the primitive; trace is an optional artifact | **Episode** is the primitive; transport is a producer; ag-ui SSE is the default capture |
| `predict → score` coupled in one pass | Capture and scoring **decoupled**; Episodes persisted; **replay** is first-class |
| Flat list of scorers ported from the PoC | Metrics grouped by the **capability** they require; build in capability-dependency order |
| Untyped `expectations` / `trace` dicts | **Typed** Episode + layered Expectations; metrics declare inputs and self-skip |
| Judge = a model string passed to MLflow | Judge = a **controlled subsystem** (pinned model, versioned rubrics, calibration) |
| Compliance = a report view | Compliance = a **structural veto lane** with stricter thresholds + sign-off |
| "A2A-first, productionize via the A2A endpoint" | A2A is a **pluggable adapter / regression anchor**; the real backend is driven over ag-ui SSE |
