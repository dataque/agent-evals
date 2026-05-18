# HR Agent — Evaluation Metrics

This report ranks 26 candidate evaluation metrics for the HR Agent system from two parallel perspectives. The **Project Impact** ranking reflects this codebase's specifics — multi-agent orchestration (Profile / Job Discovery / Outreach / JD Generator / Candidate Search), card-driven Chainlit UX, multi-turn skill / role / outreach flows, and an internal corporate compliance posture. The **First Principles** ranking reflects general agent-evaluation reasoning — what any tool-calling agent system needs to get right, before considering product specifics. Project Impact is the primary ordering used for prioritization; First Principles rank and reasoning are appended at the end of every row as a cross-reference.

## Legend

- **# (Project Impact rank)** — primary ordering used for prioritization in this project.
- **FP rank** — the same metric's rank under the first-principles view.
- **S/M-turn** — applicability to single-turn vs multi-turn evaluation.
- **Golden?** — does it require a curated reference value per scenario?
- **Judge?** — is an LLM judge in the loop?
- **Cost** — runtime cost per evaluation: **L** (deterministic), **M** (~1 judge call), **H** (full-transcript or multi-call judge).

---

## Tier 1 — Must-have agent metrics

| # | Metric | Also known as | Description | S/M-turn | Golden? | Judge? | Cost | Project reasoning | FP rank | FP reasoning |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | TSR (Task Success Rate) | Agent Goal Accuracy (Ragas); TaskCompletion (DeepEval) | Did the agent achieve the user's goal end-to-end | Both | Yes | Yes | M | The headline product KPI. Every scenario in `eval_scenarios.md` already encodes a success criterion, so this is the rolled-up pass/fail signal product and stakeholders track. | 1 | The end-to-end "did it work" outcome that every other metric decomposes into. |
| 2 | Tool Trace F1 | ToolCorrectness (DeepEval); ToolCallAccuracy (Ragas) | Precision/recall of expected vs actual tool calls | Both | Yes | No | L | Tool calls are the agent's only side-effect surface — invoking `edit_skills` instead of `search_roles` flips "modify profile" into "find jobs". Also surfaces orchestrator routing mistakes between the specialist agents. | 2 | Tool selection — the layer of correctness immediately after intent. |
| 3 | Tool Argument Correctness | ToolCorrectness w/ params (DeepEval) | Right values bound to right tools | Both | Yes | Optional | L–M | A confirm tool firing with the wrong skill name silently persists wrong data on the user's profile. The toolset is heavily parameterized (skill names, role IDs, divisions, recipient personas), so argument errors are quiet and high-impact. | 3 | Argument binding — right tool with wrong args is still a wrong action. |
| 4 | Answer Equivalence | Correctness (MLflow); GEval-correctness (DeepEval); score_model (OpenAI) | LLM-judged semantic match to a reference answer (precision: of what the agent said, was it correct?) | Both | Yes | Yes | M | The agent's free-form text varies in phrasing — keyword checks miss tone and personalization regressions. An LLM judge can recognize that "Skills saved" and "Profile updated with skills" are equivalent while flagging a subtly wrong summary. | 4 | Reference-based output correctness — the primary text-quality axis. |
| 5 | Format / Card Correctness | JSON Correctness, DAG metric (DeepEval) | Right Chainlit UI element with required fields populated | Single | Yes | No | L | The product is card-driven — JobCard, ProfileScore, DraftMessage, SkillsCard, CandidateCard. Emitting plain text where a card is expected (or a card with missing fields) breaks the experience even when the underlying language is correct. Cards are the primary product surface. | 11 | Format / schema correctness — generically mid-tier; weight depends on how UI-driven the product is. |
| 6 | Faithfulness | Faithfulness (Ragas, DeepEval); Hallucination — inverse (DeepEval) | Claims supported by tool outputs — no fabrication | Both | No | Yes | M | Inventing a role title, manager name, recruiter, or skill is a real trust-and-compliance problem in an HR context. Tool outputs are the source of truth and the answer must trace back to them. | 7 | Grounding / no-fabrication — universal correctness axis for tool-output-summarizing agents. |
| 7 | Safety / Guardrails | Safety, Guidelines (MLflow); Aspect Critic safety (Ragas) | No PII / confidential-data / policy breaches | Both | No | Yes | M | Internal corporate deployment carries a strict compliance bar — leaking PII or confidential data is a veto-power failure. A single breach can sink the deployment regardless of how every other metric scores. | 8 | Policy compliance — universal guardrail; binary failure mode rather than primary diagnostic. |
| 8 | Conversation Completeness | ConversationCompleteness (DeepEval) | Every user intent in the transcript gets a response (recall: of what the user asked, did each get addressed?) | Multi only | Optional | Yes | M–H | The flagship scenarios are multi-turn (skills modify → confirm → role match → role question → outreach). This metric verifies the full journey lands, not just one good-looking turn in the middle. | 5 | Multi-turn outcome metric — extends TSR into conversational settings. |
| 9 | Knowledge Retention | KnowledgeRetention (DeepEval) | Agent uses information shared in earlier turns | Multi only | No | Yes | M | Multi-turn coherence — the agent must recall the user's name, role, and recently-modified skills across turns without re-prompting the user for them. | 6 | Memory / coherence axis for any multi-turn agent — penalizes re-asking and forgetting. |
| 10 | Refusal Correctness | GEval-refusal rubric; Aspect Critic refusal-quality | Refuses out-of-scope / unsafe input + correct redirect | Single | Yes | Yes | M | The dataset includes refusal scenarios (other employees' details, salary, prompt-injection attempts). What's tracked is not only "did it refuse" but also "did it offer the right next step" (e.g., redirect to MyCareer). The redirect copy is itself part of the compliance behavior. | 12 | Refusal handling — applies to a subset of inputs; mid-tier diagnostic. |
| 11 | Topic Adherence | TopicAdherence (Ragas, DeepEval) | Stays inside HR / career scope | Both (Multi preferred) | No | Yes | M | The agent's allowed scope is narrow. Refusal Correctness handles point-cases; Topic Adherence catches gradual drift across a long conversation. | 9 | Scope adherence — universal axis; weight depends on how tight the allowed scope is. |
| 12 | Audit Log / Action Taken Correctness | (project-specific; ToolCorrectness applied to action tools) | Correct action recorded with right step indicator | Single | Yes | No | L | The step indicator string ("Action taken: added confirmed skills to Arlotto's profile") is what users read to confirm what was actually done on their behalf — a visible HITL trust surface and an audit-trail line item for compliance review. | 14 | Operational correctness — domain-specific; mid-tier for general agents. |
| 13 | Latency | Response time, P50 / P95 | Wall-clock per turn | Both | No | No | L | Operational signal — fits production monitoring more naturally than the eval runner. | 20 | Performance metric — non-quality axis tracked separately. |
| 14 | Token / Cost | Cost per scenario | Tokens and dollar cost | Both | No | No | L | Operational signal — catches regressions when models, prompts, or middleware change. | 21 | Economic metric — non-quality axis tracked separately. |

---

## Tier 2 — Nice-to-have agent + ops metrics

| # | Metric | Also known as | Description | S/M-turn | Golden? | Judge? | Cost | Project reasoning | FP rank | FP reasoning |
|---|---|---|---|---|---|---|---|---|---|---|
| 15 | Role Adherence | RoleAdherence (DeepEval) | Maintains the assistant persona | Both (Multi preferred) | No | Yes | M | Brand-voice quality — staying in the helpful career-assistant persona. Important but rarely catastrophic; a tone slip doesn't usually break the user's task. | 10 | Persona consistency — universal for assistants but typically less critical than functional correctness. |
| 16 | G-Eval / Rubric Scoring | GEval (DeepEval); AspectCritic / RubricsScore (Ragas); Guidelines (MLflow); score_model (OpenAI) | Custom-rubric LLM judge | Both | No | Yes | M | The catch-all judge for project-specific quality criteria — personalized greeting, button presence on cards, presence of "Download to send", correct redirect copy, and similar one-off checks. Many such rubrics are expected to accumulate. | 17 | Generic flexible LLM-judge mechanism — meta-tool rather than a specific quality axis. |
| 17 | Step / Tool-Call Efficiency | StepEfficiency, ToolCallEfficiency (DeepEval / MLflow) | Reaches the goal in minimal steps | Both | Optional | No | L | Catches thrashing — e.g. the agent calling `search_roles` three times before answering. A cost / latency proxy more than a correctness signal. | 16 | Operational efficiency — secondary to correctness. |
| 18 | Plan Quality | PlanAdherence (agent frameworks) | Reasoning trajectory follows a sensible plan | Both | Optional | Yes | M | Useful for diagnosing routing decisions in the orchestrator; Tool Trace F1 already covers most of the same ground. | 15 | Trajectory / plan quality — useful diagnostic, redundant when Tool Trace F1 is in place. |
| 19 | Answer Relevancy | AnswerRelevancy (Ragas, DeepEval); RelevanceToQuery (MLflow) | Reference-free relevance to the query | Both (Single preferred) | No | Yes | M | A fallback when no golden answer is available. Most current scenarios already have a reference, so Answer Equivalence subsumes most of the use. | 13 | Reference-free answer quality — broadly applicable when references are unavailable. |
| 20 | Bias (HR-specific) | BiasMetric (DeepEval); Aspect Critic bias rubric | No demographic bias in suggestions | Both | No | Yes | M | The agent must not infer protected attributes from a name or bias role / skill recommendations. Specialized; the current dataset only lightly targets this and would need additional adversarial scenarios to be load-bearing. | 18 | Fairness axis — relevant for systems making consequential recommendations. |
| 21 | String Check / Must-Contain | `string_check` (OpenAI); substring presence | Substring presence / absence | Single | Yes | No | L | Already wired into the dataset via `response_must_contain`. Coarse but cheap and zero-flake — a reasonable smoke-test layer below the LLM-judge metrics. | 19 | Cheap deterministic check — universally useful as a low-cost layer. |

---

## Tier 3 — RAG-flavored (low priority — no retriever in this system)

| # | Metric | Also known as | Description | S/M-turn | Golden? | Judge? | Cost | Project reasoning | FP rank | FP reasoning |
|---|---|---|---|---|---|---|---|---|---|---|
| 22 | Context Precision | ContextPrecision (Ragas) | Precision of retrieved context | Single | Yes | Yes | M | There is no vector retriever in this system — Faithfulness against tool outputs (Tier 1 #6) already covers grounding. | 22 | Critical for RAG systems with a vector retriever; conditional on architecture. |
| 23 | Context Recall | ContextRecall (Ragas) | Coverage of retrieved context | Single | Yes | Yes | M | Same — not applicable without a retriever. | 23 | Same — RAG-architectural dependency. |
| 24 | Retrieval Relevance | RetrievalRelevance (MLflow) | Relevance of retrieved chunks to the query | Single | No | Yes | M | Same. | 24 | Same. |
| 25 | Retrieval Sufficiency | RetrievalSufficiency (MLflow) | Whether retrieved context is sufficient to answer | Single | No | Yes | M | Same. | 25 | Same. |
| 26 | Noise Sensitivity | NoiseSensitivity (Ragas) | Robustness to noisy / irrelevant chunks | Single | Yes | Yes | M | Same. | 26 | Same. |

---

## Excluded (not relevant to this project)

BLEU, ROUGE, CHRF, METEOR, GLEU, Exact Match, Non-LLM string similarity (n-gram metrics on free-form responses), SQL evaluators, Multimodal Faithfulness / Relevance, standalone Toxicity (already covered by Safety / Guardrails), Summarization Score, Label Model graders.
