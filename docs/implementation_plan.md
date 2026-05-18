# Independent eval framework — A2A first, AGUI later

## Context

The HR Agent PoC at `/Users/neo/projects/chat-evals/` proves an MLflow-driven A2A eval works for a tool-calling chat agent. We now need that capability productionized for `backend` (Spring Boot 3 + Spring AI 1.x), which exposes both an ag-ui SSE endpoint (`/api/v1/bff/ai/agent/sse`) and an A2A JSON-RPC endpoint (`/api/v1/bff/ai/agent/a2a`). The A2A endpoint is structurally identical to what chat-evals already evaluates, so it is the fastest path to a production eval; AGUI follows in a later plan.

Design constraints (per user):

- **New independent repo** under `projects/` (proposed name: `agent-evals`).
- **No changes** to `chat-evals` — it stays frozen as the PoC reference.
- **Same tech stack** as chat-evals: Python, MLflow, plus DeepEval / Ragas third-party integrations.
- **Protocol-pluggable**: A2A first; AGUI is a later phase.
- **Project-pluggable**: HR Agent PoC = smoke-test plug-in; backend = production plug-in (A2A target in Phase 1).

Outcome: a single `agent-evals` package + repo any agent project can install and consume; A2A protocol fully supported in Phase 1; backend productionized end-to-end via its A2A endpoint; AGUI scaffolded for Phase 2.

---

## Repo layout (proposed)

```
projects/agent-evals/                       # NEW REPO
├── pyproject.toml
├── README.md
├── .env.example
├── docs/
│   ├── metrics.md                          # ← chat-evals/docs/eval_metrics_prod.md
│   ├── metrics_poc_reference.md            # ← chat-evals/docs/eval_metrics_poc.md
│   ├── mlflow_mapping.md                   # ← chat-evals/docs/mlflow_metrics_feasibility.md
│   ├── a2a_protocol.md                     # ← chat-evals/docs/a2a_response_v1.md
│   ├── plugin_guide.md
│   └── adr/001-protocol-adapter.md
├── agent_evals/                            # CORE FRAMEWORK
│   ├── core/
│   │   ├── protocol.py                     # ProtocolAdapter ABC
│   │   ├── trace.py                        # Trace data model
│   │   ├── scorer.py                       # Scorer base + registry
│   │   ├── dataset.py                      # Dataset format
│   │   └── project.py                      # Project plug-in interface
│   ├── protocols/a2a/                      # Phase 1
│   │   ├── client.py
│   │   ├── adapter.py
│   │   └── schemas/v1.json
│   ├── scorers/
│   │   ├── builtin.py                      # MLflow native (Correctness, Safety, ...)
│   │   ├── text.py                         # response_completeness
│   │   ├── trace_aware.py                  # tool_trace_f1, tool_argument_correctness, ...
│   │   └── schema_adherence.py             # Phase 2
│   ├── runners/mlflow_runner.py
│   ├── auth/{base,static,oauth2}.py
│   ├── harness/identity_isolation.py       # Phase 2
│   └── cli/__main__.py
├── projects/                               # PROJECT PLUG-INS
│   ├── hr_agent_poc/
│   │   ├── pyproject.toml
│   │   ├── targets.yaml
│   │   └── agent_evals_hr_poc/
│   │       ├── datasets.py                 # ← copy from chat-evals
│   │       └── scorers.py
│   └── backend/
│       ├── pyproject.toml
│       ├── targets.yaml
│       └── agent_evals_backend/
│           ├── datasets.py
│           ├── scorers.py
│           └── schemas/                    # Tool-result JSON Schemas
├── tests/
└── .github/workflows/
```

---

## Documentation transfer (read-only copies; no chat-evals modification)

| Source (chat-evals) | Destination (agent-evals) |
|---|---|
| `docs/eval_metrics_prod.md` | `docs/metrics.md` |
| `docs/eval_metrics_poc.md` | `docs/metrics_poc_reference.md` |
| `docs/mlflow_metrics_feasibility.md` | `docs/mlflow_mapping.md` |
| `docs/a2a_response_v1.md` | `docs/a2a_protocol.md` |
| `evals/schemas/a2a_response.v1.json` | `agent_evals/protocols/a2a/schemas/v1.json` |

Rewrite path references inside the copied docs from `evals/...` to `agent_evals/...`. Move HR-Agent-specific sections into `projects/hr_agent_poc/README.md`.

---

## Change Requests

### Phase 1 — MVP framework + A2A productionized on backend

**CR#1 — Bootstrap `agent-evals` repo**
Initialize new git repo at `/Users/neo/projects/agent-evals/`. Add `pyproject.toml` (deps: `mlflow[databricks]>=3.10`, `pyyaml`, `pysqlite3-binary`, `python-dotenv`, `nest-asyncio`, `requests`, `jsonschema`, `pydantic`, `msal`; dev deps: `pytest`, `ruff`, `mypy`). Add `.env.example` (mirroring chat-evals), `.gitignore`, `LICENSE`, empty package skeleton, pre-commit config.

**CR#2 — Core abstractions (`agent_evals/core/`)**
Define framework contracts. Every later CR depends on this.
- `protocol.py`: `ProtocolAdapter` ABC with `send(request, context) -> Response`. `Request` = `{question, thread_id, metadata}`; `Response` = `{text, trace, artifacts, metadata}`.
- `trace.py`: `Trace` dataclass — `events`, `tool_calls`, `tool_results`, `metadata`. Shape compatible with chat-evals' v1 trace.
- `scorer.py`: `Scorer` Protocol matching `mlflow.genai.scorers.@scorer` signature.
- `dataset.py`: `Dataset` schema — single-turn / multi-turn items + `expectations`.
- `project.py`: `Project` ABC — `name`, `datasets`, `scorers`, `targets`, `tool_schemas`.

**CR#3 — A2A protocol adapter (`agent_evals/protocols/a2a/`)**
Port the working A2A code from `chat-evals/evals/hr_benchmarker/a2a_client.py`.
- `client.py`: HTTP JSON-RPC client (`message/send` method).
- `adapter.py`: `A2AAdapter(ProtocolAdapter)` wrapping the client + thread management (UUID factory for fa, GraphQL for bff).
- `schemas/v1.json`: copy from `chat-evals/evals/schemas/a2a_response.v1.json`.
- Unit tests against the v1 fixture (port `chat-evals/evals/tests/test_a2a_client_v1.py`).

**CR#4 — Built-in MLflow scorers (`agent_evals/scorers/builtin.py`)**
Port `get_builtin_scorers()` from `chat-evals/evals/scorers.py:254-307`. Returns `Correctness`, `RelevanceToQuery`, `Safety`, plus a `Guidelines` factory. Move the chat-evals project-specific rubrics (`professional_tone`, `hr_relevance`, `data_privacy`) into the HR Agent PoC project plug-in — keep the core scorer module project-agnostic.

**CR#5 — Text + trace-aware scorers (`agent_evals/scorers/text.py`, `trace_aware.py`)**
Port the seven custom scorers from `chat-evals/evals/scorers.py:94-247`:
- `text.response_completeness`
- `trace_aware.tool_trace_f1`, `tool_argument_correctness`, `step_efficiency`, `plan_quality`, `audit_log_action_taken`
- `trace_aware.card_format_correctness` (kept for A2A; superseded for ag-ui projects in CR#13)
Refactor helpers (`_events`, `_tool_calls`, `_routes`, `_f1`) from `chat-evals/evals/scorers.py:59-87` into `core/trace.py` as `Trace` methods.

**CR#6 — MLflow runner (`agent_evals/runners/mlflow_runner.py`)**
Extract `HRBenchmarker` from `chat-evals/evals/hr_benchmarker/benchmarker.py` into a generic `MLflowRunner`. Accepts `protocol_adapter`, `dataset`, `scorers`, `n_trials`, `experiment_name`, `hyperparameters`. Manages `contextId` / thread for multi-turn (port the multi-turn loop). Returns `EvaluationResult` per agent + summary dict.

**CR#7 — Authentication adapter (`agent_evals/auth/`)**
- `base.py`: `AuthProvider` ABC returning headers dict.
- `static.py`: bearer token / function key (chat-evals `fa` target style).
- `oauth2.py`: Entra ID JWT via `msal`. Inputs: tenant id, client id, scope, optional cache path. Required for the backend target.

**CR#8 — Project plug-in mechanism + CLI (`agent_evals/core/project.py`, `agent_evals/cli/__main__.py`)**
Project discovery via Python entry points (`pyproject.toml [project.entry-points."agent_evals.projects"]`) with a path-based fallback (`--project-path /path`). Each project provides `datasets`, `scorers`, `targets`, optional `tool_schemas`. CLI ported from `chat-evals/evals/run.py` (drop AICE-specific code). Args: `--project`, `--target`, `--scorers`, `--n-trials`, `--token`/`--auth-profile`, `--base-url`, `--hyperparameters`, `-v`.

**CR#9 — HR Agent PoC project plug-in (`projects/hr_agent_poc/`)**
Smoke-test plug-in proving the framework reproduces chat-evals results.
- Copy `PROFILE_SKILLS_DATASET` and any project-specific scorers/rubrics from `chat-evals/evals/datasets.py` and `chat-evals/evals/scorers.py`.
- Copy `chat-evals/evals/targets.yaml` to `projects/hr_agent_poc/targets.yaml`.
- **Verification gate**: `python -m agent_evals --project hr_agent_poc --target fa --scorers all` produces results numerically equivalent to chat-evals' baseline (within LLM-judge variance for judged scorers; exact match for deterministic scorers).

**CR#10 — backend project plug-in via A2A (`projects/backend/`)**
Production target. Uses backend's `/api/v1/bff/ai/agent/a2a` endpoint with Entra ID JWT.
- `agent_evals_backend/__init__.py` — `Project` instance.
- `agent_evals_backend/datasets.py` — 8–10 initial scenarios, adapted from `chat-evals/docs/eval_scenarios.md` for backend tool names (`suggest_skills`, `suggest_requisitions`, `analyze_talent_profile`, `draft_message`, `save_skills`, `emit_followups`).
- `agent_evals_backend/scorers.py` — UBS-specific `Guidelines` rubrics: PII taxonomy (GPN, employee number, GCRS) plus persona quotes from `backend/src/main/resources/agents/OrchestratorAgent.md`.
- `targets.yaml` — `dev`, `staging`, `prod` entries pointing at `/api/v1/bff/ai/agent/a2a`, with `auth: oauth2-entra` reference.
- **Verification gate**: `python -m agent_evals --project backend --target dev --scorers builtin --auth-profile entra-dev` produces a green MLflow run against live backend dev with a real JWT.

**CR#11 — Documentation transfer + README**
Copy and re-frame the four docs from `chat-evals/docs/` (see table above) into `agent-evals/docs/`. Author `README.md` (framework overview, install, CLI, A2A vs AGUI status). Author `docs/plugin_guide.md` (how to add a new project plug-in). Add `docs/adr/001-protocol-adapter.md` capturing the protocol-pluggable design decision.

---

### Phase 2 — Production hardening for backend (post-MVP)

**CR#12 — backend eval-mode coordination CRs (cross-team)**
Open tickets in the `backend` repo for the prod-doc enablers (`docs/metrics.md` §9). Tracked here as plan items; executed in `backend`:
- backend `eval-tap`: SSE event-capture filter or `AgentStreamer` debug subscriber (enables full trace capture for trace-aware scorers).
- backend `usage-extract`: pull `ChatResponse.metadata.usage` into the response metadata (enables Cost metric).
- backend `correlation-id`: per-request correlation ID through advisors and tool callbacks.
- backend `x-eval-mode`: request header that suppresses irreversible side effects (send email, etc.).
- backend `eval-jwts`: sandbox tenant / pre-signed test JWTs for the identity-isolation harness.

**CR#13 — Tool Result Schema Adherence scorer (`agent_evals/scorers/schema_adherence.py`)**
Validate each tool result payload against the JSON Schema declared in the project plug-in (`projects/backend/.../schemas/`). Per `docs/metrics.md` Tier 1 #4. Supersedes `card_format_correctness` for projects that declare schemas.

**CR#14 — Identity / Tenant Isolation harness (`agent_evals/harness/identity_isolation.py`)**
Dual-JWT replay runner: for each scenario, run twice with `eval-user-A` and `eval-user-B`; assert no cross-tenant data leakage in tool results. Depends on CR#12 `eval-jwts`.

**CR#15 — CI / regression harness**
- `.github/workflows/ci.yml` — pytest on every push.
- `.github/workflows/nightly_eval.yml` — full backend eval against `dev` nightly; publish MLflow artifacts.
- `.github/workflows/compliance.yml` — separate workflow gating on Safety (#7), Identity Isolation (#8), Refusal (#9), Bias (#15) from `docs/metrics.md`. Veto-power gate; blocks production rollouts.

---

### Phase 3 — AGUI (separate plan)

**CR#16 — AGUI adapter plan (placeholder)**
Author a separate detailed plan covering `agent_evals/protocols/agui/` (SSE consumer via `httpx`, event normalization to `Trace`), `scorers/stream_health.py` (SSE protocol invariants), and `projects/backend/targets.yaml` additions for the SSE endpoint. Out of scope for this plan; tracked here as the natural follow-up.

---

## Reuse from chat-evals (port targets, no modifications to chat-evals)

| chat-evals source | What we port |
|---|---|
| `evals/scorers.py:94-247` | All 7 custom scorers ported into `agent_evals/scorers/{text,trace_aware}.py`; helpers moved into `core/trace.py` |
| `evals/scorers.py:254-307` | `get_builtin_scorers` factory pattern; rubrics moved into project plug-ins |
| `evals/hr_benchmarker/a2a_client.py` | A2A client logic ported into `agent_evals/protocols/a2a/client.py` |
| `evals/hr_benchmarker/benchmarker.py` | `HRBenchmarker` becomes generic `MLflowRunner` |
| `evals/run.py` | CLI moves to `agent_evals/cli/__main__.py`; drop AICE-specific code |
| `evals/targets.yaml` | Format kept; ownership moves into each project plug-in |
| `evals/schemas/a2a_response.v1.json` | Verbatim copy into `agent_evals/protocols/a2a/schemas/v1.json` |
| `evals/datasets.py` (`PROFILE_SKILLS_DATASET`) | Copy into `projects/hr_agent_poc/agent_evals_hr_poc/datasets.py` |
| `evals/tests/test_scorers_trace.py` | Port into `tests/test_scorers_trace_aware.py` |
| `docs/eval_metrics_prod.md` + 3 sibling docs | Copy to `agent-evals/docs/`; rewrite path references |

---

## Verification (end-to-end)

After **CR#9** (HR Agent PoC smoke test):
1. Run `python -m agent_evals --project hr_agent_poc --target fa --scorers all` and compare metric scores row-by-row against chat-evals' baseline run on the same dataset. Variance within LLM-judge noise on judged scorers; deterministic scorers exact match.

After **CR#10** (backend production target):
2. `python -m agent_evals --project backend --target dev --scorers builtin --auth-profile entra-dev` runs green against live backend dev. Confirm: Entra JWT acquired, A2A request reaches `/api/v1/bff/ai/agent/a2a`, response parsed (text + artifacts + trace), MLflow run created with judged scorers.

After **CR#11** (documentation transfer):
3. `diff chat-evals/docs/eval_metrics_prod.md agent-evals/docs/metrics.md` shows only path-reference rewrites and plug-in-example differences; metric ranking and tier structure identical.

After **CR#13** (schema-adherence scorer):
4. Deliberately rename a field in one of backend's Java records (in a test branch), re-run eval, confirm schema-adherence metric drops.

After **CR#14** (identity isolation):
5. Run isolation harness against baseline → zero cross-tenant leakage; introduce a partition-key bug in a test branch → metric fails.

After **CR#15** (CI):
6. Introduce a fake PII string in a dataset; confirm the compliance workflow blocks the run.

---

## Out of scope (explicit non-goals)

- **AGUI protocol adapter** — Phase 3, separate plan file (see CR#16).
- **Modifications to `chat-evals`** — frozen as PoC reference; all transfers are copies.
- **Replay corpus / shadow eval infrastructure** — production-traffic replay deferred.
- **Production rollout of backend eval-mode hooks** — tracked under CR#12 but executed in the `backend` repo.
- **Cross-judge bias studies** — deferred until baseline numbers stabilize.
- **Refactoring chat-evals onto the new framework** — possible later; not in this plan.
