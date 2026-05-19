# HR Agent (Prod) — Evaluation Metrics

This is a fresh ranking of the 26 candidate evaluation metrics for the **production** HR Agent — `frontend` (React/Next.js + `@ag-ui/client` + `@assistant-ui/react` over Server-Sent Events) talking to `backend` (Spring Boot 3 + Spring AI 1.x, orchestrator + three local subagents on Azure OpenAI `gpt-5.2`, persisted in Cosmos DB). It replaces `eval_metrics_dev.md`, which was written for the in-process Chainlit/A2A PoC.

Where the PoC could pretend the agent was a tidy request/response unit with named, schema-versioned artifacts, the prod system is **a streaming event protocol with hand-maintained typed contracts on both sides of a Java/TypeScript boundary — and no built-in telemetry, audit log, token tracking, or safety filter**. CRs to `frontend` and `backend` are permitted, so this analysis is opinionated about which prod changes pay for themselves once an eval harness exists.

---

## 1. Architecture deltas that drive metric design

| Dimension | PoC (chat-evals) | Prod (frontend + backend) | Eval consequence |
|---|---|---|---|
| Wire protocol | A2A JSON-RPC, single response | ag-ui SSE event stream | Tool-trace, latency, and card metrics now derive from event reconstruction, not a flat envelope |
| Card identity | `artifact.name` + `schema:"hr-agent/JobCard@v1"` | **Tool name only** — no schema id on the wire | Format-correctness must validate against a schema we **bring**, not one the server declares |
| Tool result types | A2A v1 JSON Schema fixture | Hand-maintained TS generics in `tools/*.tsx` + Java records in `backend`; no shared schema | Schema-drift between FE and BE is the #1 silent failure mode — and a new must-have metric |
| Persistence | Stateless | Cosmos DB `thread` + `message` containers, hierarchical partition `/userId/threadId/agentId`, 30-day TTL | Knowledge-retention checks become *concrete* (read what was persisted), and eval data lifecycle is separate from prod TTL |
| Identity | Optional `hr-agent.user_id` metadata | OAuth2 JWT (Entra ID), `ubs_auth_gpn` claim → tool context | Cross-user data isolation becomes a first-class metric — partition-key correctness gates everything |
| Trace artifact | `execution_trace` named artifact | **None** — must reconstruct from ag-ui events at the SSE boundary | Every trace-aware scorer (#2, #3, #12, #15, #16) depends on a new event-capture layer |
| Token / cost | Not in PoC | Spring AI metadata exists but **is not extracted or persisted** | Cost metric requires a `backend` CR; otherwise unmeasurable |
| Audit log | Synthesized from trace | **None** — only Cosmos `@CreatedBy/Date` columns | Action-Taken Correctness either degrades to "did the mutating tool fire" or waits on an audit-table CR |
| Safety filter | Not in PoC | **None** — no PII scrubbing, no prompt-injection defence | Safety/Guardrails moves from "verify policy" to "detect violations end-to-end" |
| Models | gpt-4o judge against unspecified agent | `gpt-5.2` agent runtime, judge model TBD | Eval judge model choice matters; cross-judge variance becomes part of the eval design |
| Concurrency | Single offline run | Multi-user, multi-thread, real load on shared infra | Concurrent-session isolation and Cosmos optimistic-lock correctness become real risks |
| Compliance bar | Internal demo | UBS production (financial institution) | Bias, privacy, and refusal correctness all become veto-power gates, not nice-to-haves |
| Real users | None | Real career outcomes (role matches, outreach drafts) | Real consequences for false positives; eval needs a feedback signal from production |

---

## 2. Legend

- **# (Prod Project Impact rank)** — primary ordering used for prioritization in production.
- **Also known as** — synonyms / equivalents across popular eval frameworks (DeepEval, Ragas, MLflow, OpenAI Evals).
- **Dev rank** — same metric's rank in `eval_metrics_dev.md` for cross-reference.
- **S/M-turn** — applicability to single-turn vs multi-turn evaluation.
- **Golden?** — does it require a curated reference value per scenario?
- **Judge?** — is an LLM judge in the loop?
- **Cost** — runtime cost per evaluation: **L** (deterministic), **M** (~1 judge call), **H** (full-transcript or multi-call judge).
- **MLflow path** — how this metric is implemented in the MLflow runner: *MLflow native*, *DeepEval* (third-party integration `mlflow.genai.scorers.deepeval`), *Custom `@scorer`*, or *Operational* (autolog / span attributes).
- **Specific scorer** — concrete scorer class / function / implementation strategy.
- **Δ rationale** — what changed for prod vs the dev/PoC ranking.
- **Quality concerns** — implementation risks, judge limitations, or known gaps for this metric in the prod stack.
- **Project reasoning** — durable justification for why this metric matters in the `frontend` + `backend` system (independent of rank changes).
- **CR** — production-system change required to make this metric work: **—** none, **BE** backend only, **FE** frontend only, **Both** both, **Data** eval-data layer only.

---

## 3. Tier 1 — Must-have agent metrics

| # | Metric | Also known as | Description | S/M-turn | Golden? | Judge? | Cost | MLflow path | Specific scorer | Dev rank | Δ rationale | Quality concerns | Project reasoning | CR |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | TSR (Task Success Rate) | Agent Goal Accuracy (Ragas); TaskCompletion (DeepEval) | End-to-end goal completion | Both | Yes | Yes | M | DeepEval | `TaskCompletion` / `GoalAccuracy` | 1 | Unchanged headline KPI. Judge prompt must be re-grounded against the SSE final state (last `TEXT_MESSAGE_END` + persisted assistant message in Cosmos), not the A2A `result.text`. | Judge sees only the final text; under-counts mid-conversation drops (use #10 for that). Multi-turn transcript pass-through needs a spike. | Whether the user reached the end state they came for through the orchestrator + subagent chain — the rolled-up product signal stakeholders track. | Data |
| 2 | Tool Trace F1 | ToolCorrectness (DeepEval); ToolCallAccuracy (Ragas) | Precision/recall of expected vs actual tool calls | Both | Yes | No | L | MLflow native (post-trace) | `ToolCallCorrectness` | 2 | Same metric, new source. Trace must be reconstructed by subscribing to `TOOL_CALL_START`/`END` events at the SSE boundary; today `createAgentWithEventInterception.ts:32` only listens for `TOOL_CALL_RESULT`. Add a tap. | ag-ui events must be mapped to MLflow `TOOL` spans for the native scorer; subagent indirection via the `Task` tool needs a separate routing scorer. | The only side-effect surface in the agent — wrong tool = wrong action regardless of how good the surrounding text is. Surfaces orchestrator-vs-subagent routing mistakes. | FE |
| 3 | Tool Argument Correctness | ToolCorrectness w/ params (DeepEval) | Right values bound to right tools | Both | Yes | Optional | L–M | MLflow native | `ToolCallCorrectness` w/ `expected_tool_calls` | 3 | `TOOL_CALL_ARGS` streams as deltas — eval must assemble final JSON before comparing. `save_skills` correctness gates the optimistic-lock `version` argument — wrong version silently loses the write. | Streaming deltas can be malformed mid-stream — wait for `TOOL_CALL_END` before assembling. Optimistic-lock version chain must be tracked across turns. | Confirm-tools (`save_skills`, future `send_outreach`) firing with wrong args silently persists wrong data in Cosmos; high-impact and hard to detect from the response alone. | FE |
| 4 | **Tool Result Schema Adherence** | JsonCorrectness (DeepEval); JSON Schema Adherence | Every emitted tool result validates against the schema the FE declares for that tool | Single | Yes (schema) | No | L | Custom `@scorer` | project scorer w/ AJV (FE) / `everit-org/json-schema` (BE replay) | 5 | **Reframed**. ag-ui has no `artifact.schema` field; "card identity" is just the tool name. Validate each payload against a JSON Schema derived from the FE TS generic / BE Java record. | Requires shared JSON Schema between FE and BE (CR #4); without codegen, hand-maintained schemas drift again. Only catches *structural* failures, not *content* errors. | FE-BE schema drift is the #1 silent failure mode in a typed-on-both-sides system. Tool fires (Trace F1 = pass), payload has renamed fields, card renders nothing — this metric closes the gap. | Both |
| 5 | Faithfulness | Faithfulness (Ragas, DeepEval); Hallucination — inverse (DeepEval) | Claims supported by tool outputs — no fabrication | Both | No | Yes | M | DeepEval (post-trace) | `Faithfulness` / `Hallucination` over tool spans | 6 | Higher stake in prod. Orchestrator can paraphrase tool results (e.g. `suggest_requisitions` returning 8 matches but the assistant mentions 3 with embellished titles) — no built-in grounding check exists. | MLflow's `RetrievalGroundedness` is RAG-shaped — no retriever here. DeepEval `Faithfulness` over tool-output context is the better fit; needs trace capture. | Inventing a role title, manager name, recruiter, or salary band is a real trust-and-compliance problem in HR. Tool outputs are the only source of truth; every claim must trace back. | — |
| 6 | Answer Equivalence | Correctness (MLflow); GEval-correctness (DeepEval); score_model (OpenAI) | LLM-judged semantic match to a reference answer (precision: of what the agent said, was it correct?) | Both | Yes | Yes | M | MLflow native | `Correctness` | 4 | Slight demotion. Most prod responses are short text leading into a card; free-form text carries less load than in the PoC. Useful for scripted scenarios, weaker as a top-level signal. | Cards carry the data; references in datasets may be short/repetitive. Judge can over-credit thin responses. | The orientation text wrapping each card still matters for trust ("Here are 3 matches based on your Python skill" vs hallucinated framing). | Data |
| 7 | Safety / Guardrails | Safety, Guidelines (MLflow); Aspect Critic (Ragas); DeepEval `PIILeakage` / `RoleViolation` / `NonAdvice` | No PII / confidential-data / policy breaches | Both | No | Yes | M | MLflow native + DeepEval layered | `Safety` + `Guidelines` + DeepEval `PIILeakage` / `RoleViolation` | 7 | **Promoted to a veto gate.** `backend` has zero content filtering, PII scrubbing, or prompt-injection defence. Until BE filters exist, this scorer is the *only* line of defence — must run on every row. | Generic `Safety` is too coarse for UBS-specific PII taxonomy (GPN, employee number, GCRS); layer DeepEval `PIILeakage` and custom Guidelines rubrics. | One PII / compliance breach can sink the deployment regardless of every other metric; strict internal-corporate compliance bar at a global financial institution. | BE (eventually) |
| 8 | **Cross-User Data Isolation** *(new)* | Row-level-security tests; tenant-isolation tests (multi-tenant systems) | Response surfaces only data belonging to the JWT-claimed user; partition key on every Cosmos read matches the caller's `userId` | Both | Yes | No | L | Custom `@scorer` | dual-JWT replay scorer + partition-key audit log scanner | — | **New for prod.** Hierarchical partition `/userId/threadId/agentId` is the only line preventing cross-user leakage. Inject a second test JWT, replay each scenario, assert no cross-talk. | Requires eval-mode logging of Cosmos partition keys (CR #2) and synthetic test JWTs in a sandbox tenant (CR #7); cannot run against live production traffic. | One routing bug or over-cached advisor exposes every user's profile / outreach / role match. The partition key is the only line of defence — this metric verifies it. | Data |
| 9 | Refusal Correctness | GEval-refusal rubric; Aspect Critic refusal-quality | Refuses out-of-scope / unsafe input + correct redirect | Single | Yes | Yes | M | MLflow native + custom event detector | `Guidelines` w/ refusal rubric + post-CR `REFUSAL` event check | 10 | Promoted. Today `frontend` has no structured refusal rendering (`AssistantUI/utils.ts:177` shows refusals as plain `TEXT_MESSAGE`). Until `backend` emits a `refusal` event (CR #8), eval uses NLP heuristics. | NLP refusal-detection has false positives ("I cannot find any matches" looks like a refusal); the redirect-copy quality needs its own rubric. | Refusal scenarios are common (other employees' details, salary, prompt-injection); the redirect copy is itself part of the compliance posture — getting this wrong erodes trust fast. | Both |
| 10 | Conversation Completeness | ConversationCompleteness (DeepEval) | Every user intent in the transcript gets a response (recall: of what the user asked, did each get addressed?) | Multi | Optional | Yes | M–H | DeepEval | `ConversationCompleteness` | 8 | Slight demotion. Prod flow is more sequential (one card per turn) than the PoC's tight skill→confirm→match→outreach chain; per-row TSR catches most failures. | Multi-turn pass-through unverified for SSE-source runs; Cosmos `MessageEntity` history gives a ground-truth transcript DeepEval can consume directly. | 30-day Cosmos TTL means users return to threads with new asks; sub-asks inside long sessions are easy to drop — TSR alone misses them. | Data |
| 11 | Knowledge Retention | KnowledgeRetention (DeepEval) | Agent uses information shared in earlier turns | Multi | No | Yes | M | DeepEval + Custom `@scorer` | DeepEval `KnowledgeRetention`; project scorer reading Cosmos history | 9 | **Stronger in prod.** Cosmos `MessageEntity` is the *actual* memory — Retention can be a deterministic read of what was persisted vs what was actually quoted in turn N+1. Mostly judge-free. | Cosmos read latency / consistency at eval time; deterministic variant is cheaper than the DeepEval judge but should co-exist for cross-check. | Cosmos history is ground-truth memory — eval has direct access, which is rare. Re-asking the user's name or recently-modified skills is a trust-killer. | — |
| 12 | Topic Adherence | TopicAdherence (Ragas, DeepEval) | Stays inside HR / career scope | Both | No | Yes | M | DeepEval / MLflow native | `TopicAdherence` / `Guidelines(hr_relevance)` | 11 | Unchanged. Allowed scope is still narrow; orchestrator markdown at `agents/OrchestratorAgent.md` defines voice and forbidden terms — judge against that document. | Allowed-topic list must derive from `OrchestratorAgent.md` to stay in sync; out-of-sync rubric drifts silently from production behaviour. | The agent's allowed scope is narrow at UBS; topic drift in a long conversation is a compliance and quality concern (jokes, advice, legal opinions). | — |

---

## 4. Tier 1 (continued) — Operational must-haves

| # | Metric | Also known as | Description | S/M-turn | Golden? | Judge? | Cost | MLflow path | Specific scorer | Dev rank | Δ rationale | Quality concerns | Project reasoning | CR |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 13 | Latency (TTFT + P50/P95/P99 + stream completion) | Response time (MLflow autolog); TTFT (industry term); P50/P95/P99 (SRE standard) | Wall-clock first-token, full-response, abort rate | Both | No | No | L | Operational | MLflow autolog + custom span attributes (TTFT, full-stream, abort) | 13 | **Promoted and expanded.** Streaming UX makes TTFT (`RUN_STARTED` → first `TEXT_MESSAGE_CONTENT`) the user-perceived latency; total time matters far less. | Spring AI has no OpenTelemetry today (CR #9 needed); controller / advisor must emit spans; stream-abort detection needs SSE close-handler hooks. | TTFT is what users perceive in streaming UX. Total latency matters less than first-token in a chat assistant; aborted streams are silent UX failures. | BE (timer hooks) |
| 14 | Token / Cost | Cost per scenario; Spring AI usage tracking | Tokens and $/scenario, attributed by model and by subagent | Both | No | No | L | Operational | extract `ChatResponse.metadata.usage`, attribute per subagent | 14 | **Real money.** Spring AI generation metadata is available but not extracted (`AgentConfiguration.java:114`); without a CR (#3), cost is unmeasurable. Until then: estimate via tokenizer. | Per-subagent attribution requires tracking which agent emitted each `ChatResponse`; cost rates need per-model lookup; new deployments break attribution. | Real $ in prod, paid per token; model rollouts (gpt-5.2 → successor) need cost-comparison gates; subagent attribution surfaces orchestrator cost-of-routing. | BE |
| 15 | Bias (HR-specific) | BiasMetric (DeepEval); Aspect Critic bias rubric | No demographic / protected-attribute bias in suggestions | Both | No | Yes | M | DeepEval + MLflow `Guidelines` | DeepEval `BiasMetric` + project HR-bias `Guidelines` rubric | 20 | **Promoted from Tier 2 to Tier 1.** Role/outreach surface materially affects employees' careers; bias in `suggest_requisitions` or `draft_message` is a compliance issue, not a research curiosity. | Needs adversarial-persona dataset (paired prompts varying only protected attributes); production sampling for bias raises PII concerns. | Protected attributes (gender, age, ethnicity, nationality, disability) cannot influence suggestions at a UBS-scale employer. Veto-power gate for compliance. | Data |

---

## 5. Tier 2 — Nice-to-have agent + ops metrics

| # | Metric | Also known as | Description | S/M-turn | Golden? | Judge? | Cost | MLflow path | Specific scorer | Dev rank | Δ rationale | Quality concerns | Project reasoning | CR |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 16 | Audit Log / Action Taken Correctness | (project-specific; ToolCorrectness applied to action tools) | Every mutating tool that should have fired did fire, with persisted side effect | Single | Yes | No | L | Custom `@scorer` | `@scorer` reading `audit_event` Cosmos container (post CR #6) | 12 | **Demoted until CR lands.** `backend` has no dedicated audit table — only Cosmos `@CreatedBy/Date` columns. Today the scorer can only verify `save_skills` etc. ran via `TOOL_CALL_RESULT` status (weak). With CR #6 (audit container), this becomes Tier 1. | Tool result status is a weak proxy — partial Cosmos writes go undetected; audit-container schema must be immutable, indexable, partitioned by `userId`. | Mutating actions (`save_skills`, future `send_outreach`) need verifiable receipts for compliance audit; step indicator ("Action taken: ...") is a HITL trust surface. | BE |
| 17 | G-Eval / Rubric Scoring | GEval (DeepEval); AspectCritic / RubricsScore (Ragas); Guidelines (MLflow); score_model (OpenAI) | Custom-rubric LLM judge | Both | No | Yes | M | MLflow native | `Guidelines` (extensible) | 16 | Unchanged role: catch-all for per-scenario rubrics — personalized greeting, the `Download to send` button on `DraftMessageCard`, the followup-pill ergonomics. | Rubric authoring quality varies; rubrics need ownership / review when prompts evolve in `OrchestratorAgent.md`. | Catch-all judge for project-specific quality criteria the structural scorers can't express; the workhorse for "this card needs X" rules. | — |
| 18 | Step / Tool-Call Efficiency | StepEfficiency, ToolCallEfficiency (DeepEval / MLflow) | Reaches the goal in minimal steps | Both | Optional | No | L | MLflow native (post-trace) | `ToolCallEfficiency` | 17 | Unchanged. Now derived from `STEP_STARTED`/`STEP_FINISHED` events. Catches the orchestrator thrashing the subagent loop. | Subagent loops inflate step count legitimately; threshold-tuning per scenario template needed. | Catches orchestrator thrashing — calling `suggest_requisitions` three times before answering. Latency / cost proxy more than quality signal. | — |
| 19 | Plan Quality | PlanAdherence (agent frameworks) | Reasoning trajectory follows a sensible plan | Both | Optional | Yes | M | DeepEval | `PlanQuality` / `PlanAdherence` | 18 | Slight elevation: the prod orchestrator emits `REASONING_MESSAGE_CONTENT` / `THINKING_TEXT_MESSAGE_*` events, so plan quality is now grounded in the model's own reasoning trace. | Judge prompt needs orchestrator-system-prompt context; overlaps Tool Trace F1 in coverage. | Validates orchestrator routing decisions against subagents' declared capabilities; diagnostic when TSR is failing and you need to know whether the plan was bad or execution was. | — |
| 20 | Answer Relevancy | AnswerRelevancy (Ragas, DeepEval); RelevanceToQuery (MLflow) | Reference-free relevance to the query | Both (Single preferred) | No | Yes | M | MLflow native | `RelevanceToQuery` | 19 | Unchanged: fallback for rows without a golden answer. | Mostly subsumed by Answer Equivalence (#6) when references exist. | Reference-free fallback for scenarios where authoring a golden response is expensive; smoke test against off-topic drift. | — |
| 21 | Role Adherence | RoleAdherence (DeepEval) | Maintains the assistant persona defined in `OrchestratorAgent.md` | Both | No | Yes | M | DeepEval / MLflow native | `RoleAdherence` / `Guidelines(professional_tone)` | 15 | Slight elevation: persona is now codified as a source-controlled file; judge prompt can quote it verbatim. | Persona drift hard to score deterministically; rubric should derive from `OrchestratorAgent.md` to stay in sync. | Brand-voice quality — staying in the helpful career-assistant persona. Rarely catastrophic but matters for trust at UBS. | — |
| 22 | String Check / Must-Contain | `string_check` (OpenAI); substring presence | Substring presence / absence | Single | Yes | No | L | Custom `@scorer` | `response_completeness` (already in chat-evals) | 21 | Unchanged. Cheap smoke-test layer under the LLM judges. | Low signal for free-form text; reliable for structured-field checks (specific role IDs, button labels). | Zero-flake CI gate that costs nothing to run; reasonable smoke test below the judge-based metrics. | — |
| 23 | **User Feedback Signal** *(new)* | Implicit feedback; thumbs / RLHF data; CSAT for chat | Per-message thumbs / correction collected from production users | Both | No | No | L | Operational (custom data pipeline) | thumbs-down rate aggregator + correction-text dataset builder | — | **New for prod.** frontend has no thumbs/correction UX today; adding one feeds the most decision-useful signal an eval system can have. Each thumb-down = "live failure" for offline triage. | Sampling bias (only frustrated users click); correction text may contain PII and needs redaction before review; requires UX CR. | Production users are the most honest evaluator; thumbs-down catches what scripted scenarios don't — the unknown unknowns. | FE |
| 24 | **Stream Health Detail** *(new, subset of #13)* | Protocol invariant check; SSE stream integrity | Per-event-type latency, ordering invariants, snapshot integrity | Both | No | No | L | Custom `@scorer` | project scorer over the event sequence (parser + invariant checker) | — | New. Beyond TTFT: detect malformed sequences (`TOOL_CALL_END` without matching `TOOL_CALL_START`, missing `RUN_FINISHED`, `STATE_DELTA` drift vs `MESSAGES_SNAPSHOT`). | Sensitive to `agui-server` version upgrades; needs maintenance when ag-ui adds new event types; thresholds vary across deployments. | Protocol regressions break the UX in ways quality metrics don't see; valuable when upgrading `agui-server` library or introducing custom events. | — |

---

## 6. Tier 3 — Not applicable (no retriever)

`backend` has no vector store, no retrieval-augmented generation layer. Tool results are CRUD against Cosmos DB and HR domain APIs.

| # | Metric | Also known as | Description | S/M-turn | Golden? | Judge? | Cost | MLflow path | Specific scorer | Dev rank | Status | Quality concerns | Project reasoning | CR |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 25 | Context Precision | ContextPrecision (Ragas) | Precision of retrieved context | Single | Yes | Yes | M | DeepEval | `ContextualPrecision` | 22 | N/A — no retriever | No retriever in `backend` | Faithfulness (#5) over tool outputs covers grounding for this stack | — |
| 26 | Context Recall | ContextRecall (Ragas) | Coverage of retrieved context | Single | Yes | Yes | M | DeepEval | `ContextualRecall` | 23 | N/A — no retriever | No retriever in `backend` | Same — not applicable without a retrieval layer | — |
| 27 | Retrieval Relevance | RetrievalRelevance (MLflow) | Relevance of retrieved chunks to the query | Single | No | Yes | M | MLflow native | `RetrievalRelevance` | 24 | N/A — no retriever | No retriever in `backend` | Same | — |
| 28 | Retrieval Sufficiency | RetrievalSufficiency (MLflow) | Retrieved context is sufficient to answer | Single | No | Yes | M | MLflow native | `RetrievalSufficiency` | 25 | N/A — no retriever | No retriever in `backend` | Same | — |
| 29 | Noise Sensitivity | NoiseSensitivity (Ragas) | Robustness to noisy / irrelevant chunks | Single | Yes | Yes | M | DeepEval | `ContextualRelevancy` | 26 | N/A — no retriever | No retriever in `backend` | Same | — |

If a retrieval layer is added later (e.g. semantic search over role descriptions or skill taxonomies), promote these per the dev-doc ordering and re-author the rows.

---

## 7. New metrics introduced for prod (full description)

### 7.1 Tool Result Schema Adherence (replaces #5 Format / Card Correctness)

**Why the swap.** In ag-ui the wire format identifies a card by **tool name** only; there is no `schema` field, no version marker, no envelope guarantee. The PoC's `card_format_correctness` (artifact name → schema id match) cannot run here without inventing schemas the wire doesn't carry. What the prod stack *does* have is a hand-maintained TS generic on `makeAssistantToolUI<Args, Result>` and a Java record on the backend that produces the same shape. Those two drift silently — a Java field rename ships to prod and FE renders `undefined` because the JSON parser falls back gracefully.

**Implementation.**
1. Declare each tool's `Result` shape as a JSON Schema in a shared, source-of-truth location (recommend `backend` exports it, FE consumes via codegen — see CR #4).
2. On every `TOOL_CALL_RESULT` in the eval run, validate the payload with AJV (FE) or `everit-org/json-schema` (BE replay).
3. Score: `validated_results / expected_results`. Skip when the row has no card expectations.

**What it catches that Tool Trace F1 misses.** Tool fires (F1 = pass) but the payload is `null`, has the wrong field names, or has a renamed enum value — the card rendering breaks but the trace looks fine.

### 7.2 Cross-User Data Isolation

**Why.** Cosmos partition key is `/userId/threadId/agentId`. The orchestrator passes `ToolContext.get(USER_ID)` into every tool. One wrong line — a forgotten partition filter, a leaked `threadId` from a previous request, an over-cached advisor — and user A's profile shows up in user B's chat. There is no second line of defence; this is the line.

**Implementation.**
1. Maintain at least two synthetic test JWTs (`eval-user-A`, `eval-user-B`) with disjoint Cosmos data.
2. Replay each scenario twice (once per identity). Assert no tool result for user A contains any field value scoped to user B.
3. Additionally assert every Cosmos read in the request trace used the caller's `userId` as the leading partition component. Requires CR #2 (eval-mode logging in the data access layer).

**Score.** Binary per scenario; an aggregate isolation-violation rate per dataset run.

### 7.3 User Feedback Signal

**Why.** Eval datasets capture what the team thought to test for. Production traffic captures what users actually struggle with. A thumbs-down rate broken out by tool / scenario template is the single highest-leverage signal an eval system can ingest — but only if the UX exists to collect it.

**Implementation.**
1. CR #5 in `frontend`: thumbs-up / thumbs-down + free-text affixed to each assistant message.
2. Persist to a separate Cosmos container (or Kafka topic, since backend already uses Confluent).
3. Eval ingestion: hourly sampling of thumbs-down with redaction → triage queue → labeled rows added to regression dataset.

**Score.** Production-facing metric, not a per-row scorer. Output: rolling thumbs-down rate, broken out by tool name, scenario template, and orchestrator routing decision.

### 7.4 Stream Health Detail

Subset of Latency (#13). Validates protocol-level invariants on the SSE stream:
- Every `TOOL_CALL_START` has a matching `TOOL_CALL_END` and a `TOOL_CALL_RESULT`.
- `RUN_STARTED` is followed by exactly one `RUN_FINISHED` or `RUN_ERROR`.
- `STATE_DELTA`s are consistent with the eventual `MESSAGES_SNAPSHOT`.
- No event arrives after `RUN_FINISHED`.

Cheap, deterministic. Catches `agui-server` regressions and dropped frames.

---

## 8. Suitability concerns for prod implementation

These are the constraints / risks the eval design has to absorb, ordered by severity.

1. **No token tracking on the backend (blocks #14 Cost).** Spring AI's `ChatResponse.metadata.usage` exists but is not extracted at `AgentConfiguration.java:114` or persisted anywhere. Until a CR pipes it through, cost is at best an offline tokenizer estimate. (See CR #3.)
2. **No trace artifact (blocks every trace-aware scorer: #2, #3, #16, #18, #19).** The PoC scorers consume `expectations` against an `execution_trace`. In prod that trace has to be reconstructed by subscribing to the SSE stream and capturing every event. Either the eval harness wraps the FE's `HttpAgent` (the cheap option) or `backend` publishes a second subscriber stream (the clean option — CR #1).
3. **No audit log (degrades #16 to a soft check).** No `audit_event` table; `MessageEntity` is the closest thing but isn't structured for action verification. Two paths: (a) treat tool results in the persisted message log as the audit surface (works for most tools, weak for partial-write failures), or (b) add a real audit container (CR #6).
4. **No safety filter (forces #7 to do double duty).** With zero PII scrubbing, prompt-injection defence, or output filters in `backend`, the Safety scorer is the *primary* defence in eval. Treat its failure as a release-blocker, not a metric.
5. **FE/BE schema drift (drives #4).** Tool result types are TS generics on `makeAssistantToolUI` and Java records — independently maintained. Three production tool result types in frontend (`EmitFollowupsResult`, `DraftMessageToolResult`, `analyze_talent_profile`'s payload) are hand-typed. Add codegen (CR #4) or this metric will fight drift forever.
6. **Cosmos 30-day TTL on threads/messages.** Eval needs reproducible inputs; production data ages out. Eval datasets and replay corpora must live in a separate store with their own retention. Don't depend on prod Cosmos for ground truth older than ~25 days.
7. **Real PII in production traffic.** Any feedback-loop ingestion (User Feedback Signal #23, plus shadow eval) needs redaction + access controls before rows are surfaced to model developers. Particularly: `talent_profile`, `draft_message.body` (free-text outreach), `analyze_talent_profile` salaries.
8. **Eval JWTs vs production JWTs.** Eval scenarios authenticate as synthetic users. Entra ID may not mint tokens for synthetic identities; consider an `eval-mode` profile that accepts pre-signed test JWTs in a sandbox tenant (CR #7) rather than wedging eval-only auth into the production OAuth chain.
9. **Multi-user concurrency.** Production has many simultaneous threads. Eval runs that share orchestrator caches / advisors with production traffic can interfere. Pin eval traffic to a dedicated AKS replica or run eval against a clone deployment.
10. **Subagent indirection.** Three local subagents (`talent-profile-management`, `requisition-matching`, `outreach-management`) sit behind the orchestrator. Tool-trace F1 and routing assertions must be expressed in terms of *which subagent's tool fired*, not just "a tool fired" — the orchestrator's `Task` tool is the routing surface and should be scored separately.
11. **Compliance bar (UBS).** Veto-power failure modes — Safety, Cross-User Data Isolation, Bias, Refusal Correctness — all must pass before any release. The eval system needs a separate "compliance lane" with stricter pass thresholds and required human sign-off, not the same flat dashboard as quality metrics.
12. **Model risk (gpt-5.2).** `backend` is on a single Azure OpenAI deployment. Cross-model variance (gpt-5.2 vs a hypothetical successor) is a known regression source; the eval should support `--model-override` against a stable baseline scenario set before any model swap reaches production.
13. **`emit_followups` is a side-effect tool, not an answer.** Pill quality is judged on click-through (User Feedback Signal #23), not LLM rubric. Don't conflate the two — the dev doc treats followups as part of card correctness; in prod they need a separate analytics-driven scorer.
14. **`save_skills` optimistic-lock semantics.** The tool requires the `version` argument returned by an earlier `get_talent_profile`. If Tool Argument Correctness (#3) doesn't specifically check the version chain, eval will silently bless writes that prod rejects.
15. **Cosmos write failures (transient).** Networking and throttling produce real failures. The eval should distinguish *agent quality* failures from *infrastructure* failures; add a retry-once-then-skip policy for transient Cosmos errors and tag the row rather than scoring it.

---

## 9. Required CRs to frontend / backend for full eval coverage

Listed in priority order. Each is an enabler, not a polish item.

| # | CR | Repo | Unlocks |
|---|---|---|---|
| 1 | **Event-capture tap on SSE stream.** Either an HTTP filter in `backend` that mirrors every emitted event to an eval-mode log topic (Kafka, since it's already in stack), or a debug-mode flag on `AgentStreamer.streamEvents()` that publishes to a secondary `Sinks.Many`. | backend | #2, #3, #18, #19, #24, and replay-mode eval |
| 2 | **Eval-mode data-access logging.** Capture the actual partition key used on every Cosmos read in the request. Behind a feature flag, log to a structured channel the eval harness can subscribe to. | backend | #8 (Cross-User Data Isolation) |
| 3 | **Token + cost extraction.** Pull `usage` and `model` off Spring AI's `ChatResponse.metadata`, attach to each `RUN_FINISHED` event, persist in a new `usage` field on `MessageEntity`. | backend | #14 (Cost), per-subagent attribution |
| 4 | **Shared schema for tool result types.** Generate JSON Schema from the BE Java records (use `victools/jsonschema-generator`), publish as a versioned artifact, codegen TS types in `frontend` from it. Replaces hand-maintained TS generics in `tools/*.tsx`. | Both | #4 (Schema Adherence), eliminates a class of regressions |
| 5 | **Thumbs-up/down + correction UX.** Per-assistant-message feedback widget; persists to Cosmos or Kafka. | frontend | #23 (User Feedback Signal) |
| 6 | **Structured audit log container.** New Cosmos container `audit_event` keyed by `/userId/threadId/eventId`; mutating tools (`save_skills`, future `send_outreach`) write one row each with `(tool_name, args_digest, result_status, timestamp)`. | backend | #16 (Action-Taken Correctness) |
| 7 | **Eval-mode JWT profile / sandbox tenant.** A separate Entra ID app registration or pre-signed token issuer for synthetic eval users; eval traffic uses dedicated `eval-user-*` identities with isolated Cosmos partitions. | backend (config + auth) | Eval reproducibility, Cross-User Data Isolation (#8) |
| 8 | **Structured refusal event.** When the orchestrator declines (policy, scope, safety), emit a `REFUSAL` ag-ui event (or a `STATE_DELTA` with a refusal record) instead of plain `TEXT_MESSAGE_CONTENT`. | backend + ag-ui-server fork | #9 (Refusal Correctness) — eliminates NLP detection |
| 9 | **Per-request correlation ID.** A `requestId` injected at the controller, propagated through advisors / tool callbacks, attached to every event and persisted record. | backend | All trace-aware scorers; production debugging |
| 10 | **`X-Eval-Mode` header.** Recognized by `backend` to (a) disable irreversible side effects (send-email, etc.) and (b) opt the request into verbose event capture. | Both | Safe production-shadow evaluation |

---

## 10. Implementation strategy

### 10.1 Where to hook (without a CR yet)

If CR #1 isn't shipped, the eval harness can still capture event streams by sitting between FE and BE:

- **Easiest:** wrap `@ag-ui/client`'s `HttpAgent` in a recording proxy (it already exposes `subscribe`), drive scenarios from a headless `assistant-ui` runtime, capture the full event log. This is the FE-side equivalent of the dev harness's `make_a2a_predict_fn`.
- **Cleaner:** a Java HTTP filter (`OncePerRequestFilter`) in `backend` that buffers the SSE response, tees it to a logger, returns it unchanged. Doesn't require touching agui-server internals.

### 10.2 Replay vs live runs

- **Live runs** (every scenario hits a real `gpt-5.2`) cost real tokens, vary across runs, and require Cosmos isolation. Use for: nightly regression, pre-release gates.
- **Replay runs** (recorded event streams from prior live runs are re-scored) are free, deterministic, and judge-only. Use for: scorer iteration, dataset triage, comparing two judge prompts on a frozen agent.
- Build both. The PoC has live only.

### 10.3 Eval data lifecycle

Three layers:
1. **Golden datasets** (scripted scenarios with `expectations`) — versioned in this repo, never expires.
2. **Replay corpus** (real anonymized prod sessions, redacted) — separate Cosmos container with no TTL, access-controlled.
3. **Live shadow traffic** (production requests cloned to an eval lane with `X-Eval-Mode: shadow`) — sampling-driven, used for drift detection.

### 10.4 Compliance lane

Veto metrics (#7 Safety, #8 Cross-User Data Isolation, #9 Refusal, #15 Bias) run on every release candidate against a fixed compliance dataset; a single failure blocks the release independent of all other metrics. This is structural, not advisory — wire it as a separate CI workflow whose failure is non-bypassable.

### 10.5 Judge model selection

The dev doc judged with `gpt-4o`. In prod the agent is on `gpt-5.2`. Judge-on-self is a known evaluator-bias risk; recommend a **different family or version** for the judge (a non-OpenAI model family via the appropriate Java SDK, or at minimum `gpt-5.2` agent vs `gpt-4o` judge) plus a periodic human-judge sanity sample on a slice of scored rows to estimate judge bias.

---

## 11. Excluded (not relevant to this project)

BLEU, ROUGE, CHRF, METEOR, GLEU, Exact Match, non-LLM string similarity (n-gram metrics on free-form responses), SQL evaluators, multimodal faithfulness/relevance, standalone toxicity (subsumed by Safety/Guardrails #7), summarization score, label-model graders.

---

## 12. Summary of rank changes vs `eval_metrics_dev.md`

| Dev rank | Metric | Prod rank | Δ | Reason |
|---|---|---|---|---|
| 1 | TSR | 1 | = | Unchanged headline KPI |
| 2 | Tool Trace F1 | 2 | = | Unchanged; new event source |
| 3 | Tool Argument Correctness | 3 | = | Now includes optimistic-lock version chain |
| 4 | Answer Equivalence | 6 | ↓ | Less load-bearing in card-driven prod UX |
| 5 | Format / Card Correctness | 4 (renamed) | → reframed | Replaced by Tool Result Schema Adherence |
| 6 | Faithfulness | 5 | ↑ | Higher stake; no other grounding check |
| 7 | Safety / Guardrails | 7 | = (but veto) | Promoted to a release-blocking gate |
| 8 | Conversation Completeness | 10 | ↓ | Prod flows shorter; TSR catches most |
| 9 | Knowledge Retention | 11 | ↓ slot, ↑ rigour | Now deterministic via Cosmos read |
| 10 | Refusal Correctness | 9 | ↑ | Needs CR; primary trust signal |
| 11 | Topic Adherence | 12 | ↓ slightly | Unchanged role |
| 12 | Audit Log / Action Taken | 16 | ↓ | Demoted until audit-table CR lands |
| 13 | Latency | 13 | = | Promoted in dev doc edit; expanded with TTFT |
| 14 | Token / Cost | 14 | = | Promoted in dev doc edit; blocked on BE CR |
| 15 | Role Adherence | 21 | ↓ | Same role, lower priority |
| 16 | G-Eval / Rubric | 17 | ≈ | Unchanged catch-all |
| 17 | Step Efficiency | 18 | ≈ | Unchanged |
| 18 | Plan Quality | 19 | ≈ | Slight elevation in usefulness |
| 19 | Answer Relevancy | 20 | ≈ | Fallback only |
| 20 | Bias (HR-specific) | 15 | ↑↑ | Compliance gate at UBS |
| 21 | String Check / Must-Contain | 22 | ≈ | Smoke-test layer |
| 22–26 | RAG-flavored | 25–29 | = | Still N/A |
| — | Tool Result Schema Adherence | 4 | new | Replaces Format/Card |
| — | Cross-User Data Isolation | 8 | new | Partition key correctness |
| — | User Feedback Signal | 23 | new | Production thumbs-down |
| — | Stream Health Detail | 24 | new | Protocol-level invariants |
