# HR Agent — MLflow Feasibility for the 26 Eval Metrics

The HR Agent eval framework already uses MLflow as the runner — `mlflow.genai.evaluate()` is invoked from `evals/hr_benchmarker/benchmarker.py`, with scorers defined in `evals/scorers.py` (`Correctness`, `RelevanceToQuery`, `Safety`, three `Guidelines` rules, plus a custom `@scorer response_completeness`). This report documents how each of the 26 metrics from `eval_metrics.md` maps onto MLflow — natively, via MLflow's third-party DeepEval integration, or via custom `@scorer`.

## Verdict

**All 26 metrics are implementable inside MLflow without compromising output quality**, contingent on one cross-cutting infrastructure prerequisite (tool-call traces).

- **Already wired (5 metrics):** Answer Equivalence, Safety / Guardrails, G-Eval / Rubric Scoring, Answer Relevancy, String Check — running today via `evals/scorers.py`.
- **MLflow native, not yet wired (4 metrics):** Tool Trace F1, Tool Argument Correctness, Step / Tool-Call Efficiency, Refusal Correctness — `ToolCallCorrectness`, `ToolCallEfficiency`, additional `Guidelines` rule.
- **DeepEval-via-MLflow (8 metrics):** TSR, Faithfulness, Conversation Completeness, Knowledge Retention, Topic Adherence, Role Adherence, Plan Quality, Bias — DeepEval is a first-class third-party integration (`mlflow.genai.scorers.deepeval`).
- **Custom `@scorer` (2 metrics):** Format / Card Correctness, Audit Log / Action Taken — full trace and expectations access available.
- **Operational (2 metrics):** Latency, Token / Cost — MLflow run logs / autolog.
- **Not applicable (5 metrics):** Tier 3 RAG metrics — no retriever in this system.

The single blocker is **tool-call trace capture**: ~6 metrics require MLflow traces with `TOOL` spans, and the current A2A predict path (`evals/hr_benchmarker/a2a_client.py`) only extracts the final text from the A2A response. Once trace capture is wired, every blocked metric clears.

## Cross-cutting prerequisites and concerns

**§1. Tool-call trace capture (blocker for ~6 metrics)** — `ToolCallCorrectness` and `ToolCallEfficiency` "require traces with explicit TOOL spans". A custom `@scorer` taking a `trace` parameter "cannot be used with pandas DataFrames — it needs real execution traces." The current A2A loop only extracts text, so tool spans never reach the eval runner. *Resolution:* instrument `a2a_server/executor.py` (or `agents/orchestrator/agent.py`) so each tool call emits a `mlflow.start_span(span_type="TOOL", ...)`, persisted to the same MLflow tracking server the eval runner reads.

**§2. Multi-turn pass-through inside the MLflow runner** — DeepEval conversational metrics support `window_size`, but how the full transcript reaches the wrapped DeepEval metric inside `mlflow.genai.evaluate()` is not fully documented. The `HRBenchmarker` already manages `contextId` per scenario — what is needed is a small wrapper to confirm the transcript passes through. A 30-min spike before relying on these is recommended.

**§3. Faithfulness scoping** — MLflow's native `RetrievalGroundedness` is RAG-flavored (expects retrieved context). For tool-output grounding, DeepEval's `Faithfulness` / `Hallucination` is the better fit. Once trace capture is in place, a custom `@scorer` reading tool spans is also viable.

**§4. Custom-scorer offline-only** — Per the docs, custom `@scorer` is "offline evaluation only — cannot be used with automatic / production monitoring." Fine for the eval suite; flag if any of these are later promoted into a live monitor.

**§5. Safety customization** — MLflow `Safety` is general-purpose. Org-specific compliance is already handled via `Guidelines` (custom natural-language rules: `data_privacy`, `professional_tone`, `hr_relevance`). DeepEval offers granular siblings: `PIILeakage`, `RoleViolation`, `NonAdvice`, `Misuse` — usable as layered defenses.

## Per-metric mapping

Tier divisions match `eval_metrics.md`. **Status** legend: *Already wired* — runs today via `evals/scorers.py`; *Full* — implementable directly with no quality compromise; *Partial* — implementable with a specific concern flagged in row; *Blocked on §1* — needs tool-trace prerequisite first; *N/A* — not applicable to this project.

### Tier 1 — Must-have agent metrics

Tier 1 is ordered by practical implementation priority — the two metrics already running in `evals/scorers.py` (Answer Equivalence, Safety / Guardrails) lead, with the remainder in canonical project-impact order.

| # | Metric | Also known as | Description | S/M-turn | Golden? | Judge? | Cost | MLflow path | Specific scorer | Status | Effort & relevance | Quality concerns | Project reasoning |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4 | Answer Equivalence | Correctness (MLflow); GEval-correctness (DeepEval); score_model (OpenAI) | LLM-judged semantic match to reference | Both | Yes | Yes | M | MLflow native | `Correctness` | Already wired | ★ **Quick win — already wired**. Zero new code in `evals/scorers.py`. Every scenario already carries `expected_response`. | None | Free-form text varies in phrasing — the LLM judge catches subtly wrong summaries that keyword checks miss. |
| 7 | Safety / Guardrails | Safety, Guidelines (MLflow); Aspect Critic (Ragas) | No PII / confidential / policy breaches | Both | No | Yes | M | MLflow native | `Safety` + `Guidelines` | Already wired | ★ **Quick win — already wired**. `Safety` + 3 `Guidelines` rules running in `evals/scorers.py`. Highest project relevance — compliance is veto-power. | Layer DeepEval `PIILeakage` / `RoleViolation` / `NonAdvice` for finer-grained tracking | Strict compliance bar — one breach is a veto-power failure regardless of other metrics. |
| 1 | TSR | Agent Goal Accuracy (Ragas); TaskCompletion (DeepEval) | End-to-end goal achievement | Both | Yes | Yes | M | DeepEval | `TaskCompletion` / `GoalAccuracy` | Full | Moderate effort — install `deepeval` and wire `TaskCompletion` into `evals/scorers.py`. Top KPI; high relevance. Multi-turn (§2) needs a small spike. | Multi-turn pass-through unverified — see §2 | Headline product KPI; every scenario in `eval_scenarios.md` already encodes a success criterion. |
| 2 | Tool Trace F1 | ToolCorrectness (DeepEval); ToolCallAccuracy (Ragas) | Precision/recall of expected vs actual tool calls | Both | Yes | No | L | MLflow native | `ToolCallCorrectness` | Blocked on §1 | High effort — blocked on §1 trace capture. Highest diagnostic value once unblocked. | Requires tool-trace capture in agent / A2A path | Tool calls are the only side-effect surface; surfaces orchestrator routing mistakes between Profile / Job Discovery / Outreach specialists. |
| 3 | Tool Argument Correctness | ToolCorrectness w/ params (DeepEval) | Right values bound to right tools | Both | Yes | Optional | L–M | MLflow native | `ToolCallCorrectness` w/ `expected_tool_calls` | Blocked on §1 | High effort — same §1 prerequisite. Critical relevance (silent-data-corruption risk on confirm tools). | Trace prerequisite. Alt: DeepEval `ArgumentCorrectness` | A confirm tool firing with the wrong skill name silently persists wrong data on the user's profile. |
| 5 | Format / Card Correctness | JSON Correctness, DAG metric (DeepEval) | Right Chainlit element with required fields | Single | Yes | No | L | Custom `@scorer` | Project-specific scorer over `outputs` + `expectations` | Full | Moderate effort — author a project-specific custom `@scorer` for the Chainlit card schema. Very high relevance — card-driven UX. | DeepEval `JsonCorrectness` is too generic for the Chainlit card schema | Card-driven UX (JobCard, ProfileScore, DraftMessage, SkillsCard, CandidateCard); wrong card = visibly broken UX. |
| 6 | Faithfulness | Faithfulness (Ragas, DeepEval); Hallucination — inverse (DeepEval) | Claims supported by tool outputs | Both | No | Yes | M | DeepEval | `Faithfulness` / `Hallucination` | Partial → Full after §1 | Moderate effort — DeepEval install + scorer; tool-output variant gated on §1. High relevance — fabrication risk in HR is real. | MLflow's `RetrievalGroundedness` is RAG-shaped; DeepEval or trace-aware `@scorer` is the better fit | Inventing a role title, manager, recruiter, or skill is a real trust-and-compliance problem. |
| 8 | Conversation Completeness | ConversationCompleteness (DeepEval) | Multi-turn goal fulfillment | Multi only | Optional | Yes | M–H | DeepEval | `ConversationCompleteness` | Partial — needs §2 spike | Moderate effort — DeepEval + §2 spike. High relevance — flagship multi-turn flows depend on this. | Multi-turn transcript pass-through unverified | Flagship scenarios are multi-turn; verify the full journey lands, not just one turn. |
| 9 | Knowledge Retention | KnowledgeRetention (DeepEval) | Agent uses earlier-turn info | Multi only | No | Yes | M | DeepEval | `KnowledgeRetention` | Partial — needs §2 spike | Moderate effort — DeepEval + §2 spike. High relevance — multi-turn coherence is core. | Same multi-turn concern as #8 | Agent must recall name, role, and recently-modified skills across turns without re-prompting. |
| 10 | Refusal Correctness | GEval-refusal rubric; Aspect Critic refusal-quality | Refuses out-of-scope + correct redirect | Single | Yes | Yes | M | MLflow native | `Guidelines` w/ project rubric | Full | ★ **Quick win — extends wired pattern**. Add a 4th `Guidelines` rubric in `evals/scorers.py`; no new dependency. High compliance relevance. | Add as a 4th `Guidelines` instance in `evals/scorers.py` | Refusal scenarios are explicit in dataset; the redirect copy itself is part of the compliance behavior. |
| 11 | Topic Adherence | TopicAdherence (Ragas, DeepEval) | Stays inside HR / career scope | Both (Multi preferred) | No | Yes | M | DeepEval | `TopicAdherence` | Full (partial today) | ★ **Partially wired** — `Guidelines(hr_relevance)` covers single-turn drift today. Optional DeepEval upgrade for full multi-turn TopicAdherence semantics. | Direct mapping | Narrow scope; catches gradual drift across long conversations. |
| 12 | Audit Log / Action Taken Correctness | (project-specific; ToolCorrectness on action tools) | Correct action recorded with right step indicator | Single | Yes | No | L | Custom `@scorer` | `@scorer` inspecting trace for action span | Blocked on §1 | High effort — blocked on §1, then a custom `@scorer` reading the action span. High HITL-trust relevance. | Trace prerequisite | Step indicator ("Action taken: added confirmed skills…") is a HITL trust surface and audit-trail line item. |

### Tier 2 — Nice-to-have agent + ops metrics

| # | Metric | Also known as | Description | S/M-turn | Golden? | Judge? | Cost | MLflow path | Specific scorer | Status | Effort & relevance | Quality concerns | Project reasoning |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 13 | Role Adherence | RoleAdherence (DeepEval) | Maintains assistant persona | Both (Multi preferred) | No | Yes | M | DeepEval | `RoleAdherence` | Full (partial today) | ★ **Partially wired** — `Guidelines(professional_tone)` covers persona for single-turn. Optional DeepEval upgrade for multi-turn role-violation tracking. | Direct mapping | Brand-voice quality — staying in the helpful career-assistant persona; rarely catastrophic. |
| 14 | G-Eval / Rubric Scoring | GEval (DeepEval); AspectCritic / RubricsScore (Ragas); Guidelines (MLflow); score_model (OpenAI) | Custom-rubric LLM judge | Both | No | Yes | M | MLflow native | `Guidelines` | Already wired | ★ **Quick win — already wired**. `Guidelines` mechanism running in `evals/scorers.py`; just author more rubrics. | Extend with additional rubrics as needed | Catch-all judge for project-specific quality criteria — greeting personalization, button presence on cards, redirect copy. |
| 15 | Step / Tool-Call Efficiency | StepEfficiency, ToolCallEfficiency (DeepEval / MLflow) | Reaches goal in minimal steps | Both | Optional | No | L | MLflow native | `ToolCallEfficiency` | Blocked on §1 | High effort — blocked on §1. Lower priority (cost / latency proxy more than quality). | Trace prerequisite. Alt: DeepEval `StepEfficiency` | Catches thrashing — agent calling `search_roles` three times before answering. |
| 16 | Plan Quality | PlanAdherence (agent frameworks) | Reasoning trajectory follows sensible plan | Both | Optional | Yes | M | DeepEval | `PlanQuality` / `PlanAdherence` | Full once §1 done | Moderate effort — DeepEval + §1 dependence. Diagnostic value, but overlaps Tool Trace F1. | Trace-aware metric | Diagnostic for orchestrator routing; overlaps with Tool Trace F1. |
| 17 | Answer Relevancy | AnswerRelevancy (Ragas, DeepEval); RelevanceToQuery (MLflow) | Reference-free relevance to query | Both (Single preferred) | No | Yes | M | MLflow native | `RelevanceToQuery` | Already wired | ★ **Quick win — already wired**. `RelevanceToQuery` running in `evals/scorers.py`. | None | Fallback when no golden answer exists; most current scenarios already have references. |
| 18 | Bias (HR-specific) | BiasMetric (DeepEval); Aspect Critic bias rubric | No demographic bias in suggestions | Both | No | Yes | M | DeepEval | `BiasMetric` | Full | Moderate effort — DeepEval install + `BiasMetric`. Specialized; needs adversarial scenarios in dataset to be load-bearing. | Add HR-specific `Guidelines` rubric as a complementary check | Don't infer protected attributes from a name; don't bias role / skill recommendations. |
| 19 | String Check / Must-Contain | `string_check` (OpenAI); substring presence | Substring presence / absence | Single | Yes | No | L | Custom `@scorer` | `response_completeness` | Already wired | ★ **Quick win — already wired**. Custom `response_completeness` operating on `response_must_contain` keywords. | None | Cheap, zero-flake smoke-test baseline already wired into the dataset via `response_must_contain`. |
| 20 | Latency | Response time, P50 / P95 | Wall-clock per turn | Both | No | No | L | Operational | MLflow autolog / run params | Full | Quick — built-in once tracing (§1) is on. Operational signal. | Built-in once traces captured | Operational signal — fits production monitoring more naturally than the eval runner. |
| 21 | Token / Cost | Cost per scenario | Tokens and dollar cost | Both | No | No | L | Operational | MLflow run params, span attributes | Full | Quick — built-in once tracing (§1) is on. Operational signal. | Built-in once traces captured | Operational signal — catches regressions when models, prompts, or middleware change. |

### Tier 3 — RAG-flavored (low priority — no retriever in this system)

| # | Metric | Also known as | Description | S/M-turn | Golden? | Judge? | Cost | MLflow path | Specific scorer | Status | Effort & relevance | Quality concerns | Project reasoning |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 22 | Context Precision | ContextPrecision (Ragas) | Precision of retrieved context | Single | Yes | Yes | M | DeepEval / native | `ContextualPrecision` (DeepEval) | N/A | N/A — no retriever in this system | No retriever | Faithfulness against tool outputs (Tier 1 #6) already covers grounding. |
| 23 | Context Recall | ContextRecall (Ragas) | Coverage of retrieved context | Single | Yes | Yes | M | DeepEval / native | `ContextualRecall` (DeepEval) | N/A | N/A — no retriever in this system | No retriever | Same — not applicable without a retriever. |
| 24 | Retrieval Relevance | RetrievalRelevance (MLflow) | Relevance of retrieved chunks | Single | No | Yes | M | MLflow native | `RetrievalRelevance` | N/A | N/A — no retriever in this system | No retriever | Same. |
| 25 | Retrieval Sufficiency | RetrievalSufficiency (MLflow) | Whether retrieved context is sufficient | Single | No | Yes | M | MLflow native | `RetrievalSufficiency` | N/A | N/A — no retriever in this system | No retriever | Same. |
| 26 | Noise Sensitivity | NoiseSensitivity (Ragas) | Robustness to noisy chunks | Single | Yes | Yes | M | DeepEval / native | `ContextualRelevancy` (DeepEval) | N/A | N/A — no retriever in this system | No retriever | Same. |

## Recommended implementation phases

**Phase 0 — Tool-trace capture** (unblocks 6 metrics; biggest single dependency)
- Add MLflow tracing instrumentation to `a2a_server/executor.py` so each tool invocation creates a `TOOL` span.
- Verify traces show up under the same experiment as eval runs.
- Confirm `mlflow.get_trace(trace_id)` round-trip from inside a custom `@scorer`.

**Phase 1 — Wire native MLflow scorers not yet in use** (no new dependency)
- `ToolCallCorrectness`, `ToolCallEfficiency` → `evals/scorers.py`.
- 4th `Guidelines` instance for Refusal Correctness ("response refuses out-of-scope request and offers the correct redirect").
- Extend `evals/datasets.py` expectations with `expected_tool_calls` per scenario.

**Phase 2 — Add DeepEval third-party integration** (one new dependency: `deepeval`)
- Install `deepeval`; import from `mlflow.genai.scorers.deepeval`.
- Wire: `TaskCompletion`, `Faithfulness` / `Hallucination`, `ConversationCompleteness`, `KnowledgeRetention`, `RoleAdherence`, `TopicAdherence`, `BiasMetric`, `PlanQuality` / `PlanAdherence`, `StepEfficiency` (optional alternative).
- Run §2 spike: confirm multi-turn transcript reaches DeepEval metrics correctly.

**Phase 3 — Project-specific custom @scorers**
- `card_format_correctness(outputs, expectations) → Feedback` (Format / Card).
- `action_taken_correctness(trace, expectations) → Feedback` (Audit Log).

**Phase 4 — Operational metrics** (lightest; can run anytime after Phase 0)
- Surface latency / token / cost via MLflow run params and autolog.

## Known caveats

- **Multi-turn integration mechanics** (§2) — the largest open uncertainty; warrants the spike before relying on conversational DeepEval metrics in production reports.
- **Trace propagation across the A2A HTTP boundary** — `mlflow.autolog()` for LangChain captures local spans, but the eval runner is in a separate process from the agent. Trace-id propagation through A2A headers is likely required.
- **Custom `@scorer` + DataFrame limitation** — `HRBenchmarker._collect_predictions` produces a DataFrame; trace-aware scorers may need the runner to use the predict_fn path instead.
- **`Guidelines` rubric quality** — for Refusal Correctness and Bias, the prompt rubric needs careful authoring. This is rubric-engineering work, not a framework limitation.
