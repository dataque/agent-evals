

# Metrics catalog

## 1. Overview

The metrics this eval implements, with cross-framework equivalents (DeepEval,
Ragas, MLflow, OpenAI Evals, Azure AI Foundry). 24 in-scope metrics (§3–§5) plus
one product-specific UX metric (#25, follow-up pills); retrieval metrics (§6) are
N/A — the agent has no retriever. The eval is **data-independent**: goldens
assert behaviour, not specific records, so reference-answer metrics like #6
(Answer Equivalence) are available but not exercised by the bundled HR suite.

## 2. Legend

- **# (Prod Project Impact rank)** — primary ordering used for prioritization in production.
- **Also known as** — synonyms / equivalents across popular eval frameworks (DeepEval, Ragas, MLflow, OpenAI Evals, Azure AI Foundry).
- **S/M-turn** — applicability to single-turn vs multi-turn evaluation.
- **Golden?** — does it require a curated reference value per scenario?
- **Judge?** — is an LLM judge in the loop?
- **Cost** — runtime cost per evaluation: **L** (deterministic), **M** (~1 judge call), **H** (full-transcript or multi-call judge).
- **MLflow path** — how this metric is implemented in the MLflow runner: *MLflow native*, *DeepEval* (third-party integration `mlflow.genai.scorers.deepeval`), *Custom `@scorer`*, or *Operational* (autolog / span attributes).

---

## 3. Primary — Must-have agent metrics

| # | Metric | Also known as | Description | S/M-turn | Golden? | Judge? | Cost | MLflow path |
|---|---|---|---|---|---|---|---|---|
| 1 | Task Completion | Agent Goal Accuracy (Ragas); TaskCompletion (DeepEval); Task Completion (Azure AI Foundry) | End-to-end goal completion | Both | Optional | Yes | M | DeepEval |
| 2 | Tool Selection Accuracy | ToolCorrectness (DeepEval); ToolCallAccuracy (Ragas); Tool Selection (Azure AI Foundry) | Precision/recall of expected vs actual tool calls | Both | Optional | No | L | MLflow native (post-trace) |
| 3 | Tool Argument Correctness | ToolCorrectness w/ params (DeepEval); Tool Input Accuracy (Azure AI Foundry) | Right values bound to right tools | Both | Optional | Optional | L–M | MLflow native |
| 4 | Tool Result Schema Adherence | JsonCorrectness (DeepEval); JSON Schema Adherence | Every emitted tool result validates against the schema the FE declares for that tool | Single | No (shared schema) | No | L | Custom `@scorer` |
| 5 | Faithfulness | Faithfulness (Ragas, DeepEval); Hallucination - inverse (DeepEval); Groundedness (Azure AI Foundry) | Claims supported by tool outputs - no fabrication | Both | No | Yes | M | DeepEval (post-trace) |
| 6 | Answer Equivalence | Correctness (MLflow); GEval-correctness (DeepEval); score_model (OpenAI) | LLM-judged semantic match to a reference answer (precision: of what the agent said, was it correct?) | Both | Yes | Yes | M | MLflow native |
| 7 | Safety / Guardrails | Safety, Guidelines (MLflow); Aspect Critic (Ragas); DeepEval `PIILeakage` / `RoleViolation` / `NonAdvice` | No PII / confidential-data / policy breaches | Both | No | Yes | M | MLflow native + DeepEval layered |
| 8 | Cross-User Data Isolation | Row-level-security tests; tenant-isolation tests (multi-tenant systems) | Response surfaces only data belonging to the JWT-claimed user; partition key on every Cosmos read matches the caller's `userId` | Both | No | No | L | Custom `@scorer` |
| 9 | Refusal Correctness | GEval-refusal rubric; Aspect Critic refusal-quality | Refuses out-of-scope / unsafe input + correct redirect | Single | Optional | Yes | M | MLflow native + custom event detector |
| 10 | Conversation Completeness | ConversationCompleteness (DeepEval) | Every user intent in the transcript gets a response (recall: of what the user asked, did each get addressed?) | Multi | Optional | Yes | M–H | DeepEval |
| 11 | Knowledge Retention | KnowledgeRetention (DeepEval) | Agent uses information shared in earlier turns | Multi | No | Yes | M | DeepEval + Custom `@scorer` |
| 12 | Topic Adherence | TopicAdherence (Ragas, DeepEval) | Stays inside HR / career scope | Both | No | Yes | M | DeepEval / MLflow native |

---

## 4. Primary (continued) — Operational must-haves

| # | Metric | Also known as | Description | S/M-turn | Golden? | Judge? | Cost | MLflow path |
|---|---|---|---|---|---|---|---|---|
| 13 | Latency (TTFT + P50/P95/P99 + stream completion) | Response time (MLflow autolog); TTFT (industry term); P50/P95/P99 (SRE standard) | Wall-clock first-token, full-response, abort rate | Both | No | No | L | Operational |
| 14 | Token / Cost | Cost per scenario; Spring AI usage tracking | Tokens and $/scenario, attributed by model and by subagent | Both | No | No | L | Operational |
| 15 | Bias (HR-specific) | BiasMetric (DeepEval); Aspect Critic bias rubric | No demographic / protected-attribute bias in suggestions | Both | No | Yes | M | DeepEval + MLflow `Guidelines` |

---

## 5. Secondary — Nice-to-have agent + ops metrics

| # | Metric | Also known as | Description | S/M-turn | Golden? | Judge? | Cost | MLflow path |
|---|---|---|---|---|---|---|---|---|
| 16 | Audit Log / Action Taken Correctness | (project-specific; ToolCorrectness applied to action tools) | Every mutating tool that should have fired did fire, with persisted side effect | Single | Yes | No | L | Custom `@scorer` |
| 17 | G-Eval / Rubric Scoring | GEval (DeepEval); AspectCritic / RubricsScore (Ragas); Guidelines (MLflow); score_model (OpenAI) | Custom-rubric LLM judge | Both | No | Yes | M | MLflow native |
| 18 | Step / Tool-Call Efficiency | StepEfficiency, ToolCallEfficiency (DeepEval / MLflow); Task Navigation Efficiency (Azure AI Foundry) | Reaches the goal in minimal steps | Both | Optional | No | L | MLflow native (post-trace) |
| 19 | Plan Quality | PlanAdherence (agent frameworks) | Reasoning trajectory follows a sensible plan | Both | Optional | Yes | M | DeepEval |
| 20 | Answer Relevancy | AnswerRelevancy (Ragas, DeepEval); RelevanceToQuery (MLflow); Relevance (Azure AI Foundry) | Reference-free relevance to the query | Both (Single preferred) | No | Yes | M | MLflow native |
| 21 | Role Adherence | RoleAdherence (DeepEval) | Maintains the assistant persona defined in `OrchestratorAgent.md` | Both | No | Yes | M | DeepEval / MLflow native |
| 22 | String Check / Must-Contain | `string_check` (OpenAI); substring presence | Substring presence / absence | Single | Yes | No | L | Custom `@scorer` |
| 23 | **User Feedback Signal** *(new)* | Implicit feedback; thumbs / RLHF data; CSAT for chat | Per-message thumbs / correction collected from production users | Both | No | No | L | Operational (custom data pipeline) |
| 24 | **Stream Health Detail** *(new, subset of #13)* | Protocol invariant check; SSE stream integrity | Per-event-type latency, ordering invariants, snapshot integrity | Both | No | No | L | Custom `@scorer` |
| 25 | **Follow-up Pills Correctness** *(new, product UX contract)* | Suggestion-chip / quick-reply correctness | Emitted follow-up `scenario_id` + exact pill set match the backend's pill contract | Single | No (config) | No | L | Custom `@scorer` |

---

## 6. Excluded — Not applicable (no retriever)

`backend` has no vector store, no retrieval-augmented generation layer. Tool results are CRUD against Cosmos DB and HR domain APIs.

| # | Metric | Also known as | Description | S/M-turn | Golden? | Judge? | Cost | MLflow path | Status |
|---|---|---|---|---|---|---|---|---|---|
| 25 | Context Precision | ContextPrecision (Ragas) | Precision of retrieved context | Single | Yes | Yes | M | DeepEval | N/A — no retriever |
| 26 | Context Recall | ContextRecall (Ragas) | Coverage of retrieved context | Single | Yes | Yes | M | DeepEval | N/A — no retriever |
| 27 | Retrieval Relevance | RetrievalRelevance (MLflow) | Relevance of retrieved chunks to the query | Single | No | Yes | M | MLflow native | N/A — no retriever |
| 28 | Retrieval Sufficiency | RetrievalSufficiency (MLflow) | Retrieved context is sufficient to answer | Single | No | Yes | M | MLflow native | N/A — no retriever |
| 29 | Noise Sensitivity | NoiseSensitivity (Ragas) | Robustness to noisy / irrelevant chunks | Single | Yes | Yes | M | DeepEval | N/A — no retriever |

If a retrieval layer is added later (e.g. semantic search over role descriptions or skill taxonomies), promote these per the dev-doc ordering and re-author the rows.

---
