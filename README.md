# agent-evals

A generic, framework-independent evaluation system for agentic chat systems.
It drives an agent over its live wire protocol, normalizes every run into a
transport-neutral **`RunRecord`**, and scores it against the metric catalog in
`docs/metrics.md`. MLflow is the first metrics backend, but
the core never imports it — porting to another framework means writing one
adapter, not a rewrite.

## Why the design looks like this

Two seams keep everything pluggable:

```
EvalCase ─► Runner ─► Transport.run_turn() ─► RunRecord ─► Scorer[] ─► Score[] ─► MetricsSink
                          │                       │            │                     │
                   agui_sse (primary)      normalized,   pure, no mlflow      MLflowSink / JsonlSink
                   a2a (optional)          transport-neutral
```

- **Transport seam** (`RunRecord`): scorers never know whether the run came from
  an AG-UI/SSE stream or an A2A endpoint. Swap transports without touching scorers.
- **Sink + Judge seams**: `MetricsSink` and `Judge` are abstract. MLflow, DeepEval,
  a local JSONL file, or a direct LLM call are all adapters behind them. The
  `core/`, `scorers/`, and `transport/` packages contain **zero** `import mlflow`.

## Layout

| Package | Responsibility |
|---|---|
| `agent_evals.core` | `RunRecord`, `EvalCase`, `Scorer`/`Score`, `Judge`, `MetricsSink`, `Runner`, aggregation. Framework-neutral. |
| `agent_evals.transport` | Drive the system-under-test → `RunRecord`. `agui_sse` (primary), `a2a` (optional), auth, projection. |
| `agent_evals.scorers` | One module per metric family; pure functions over `(EvalCase, RunRecord)`. |
| `agent_evals.judges` | Provider-neutral `Judge` impls: Azure OpenAI (default), MLflow-native, DeepEval. |
| `agent_evals.sinks` | `MlflowSink` (first) and `JsonlSink` (portability proof). The only place `mlflow` is imported. |
| `agent_evals.contracts` | Tool-name → JSON-Schema registry (for tool-result-schema adherence). |
| `agent_evals.datasets` | Suite loader + fact deriver (bring-your-own suite). |

## Install

```bash
pip install -e ".[dev]"          # core + tests
pip install -e ".[mlflow]"       # + MLflow sink/judge
pip install -e ".[deepeval]"     # + DeepEval judge
pip install -e ".[openai]"       # + Azure/OpenAI judge (default)
```

## Usage

Per-developer values (your user login id, tokens, judge keys) go in a gitignored **`.env`**
(copy `.env.example`). The CLI auto-loads it, and `targets.yaml` references vars as
`${VAR}` — e.g. `user_login_id: "${AGENT_EVALS_USER_LOGIN_ID}"`. So set
`AGENT_EVALS_USER_LOGIN_ID=<your real login id>` in `.env` before running.

> `docs/evaluation-setup.md` is the reference for
> authoring/extending the dataset, judge calibration, and operating the eval;
> agent defects the eval surfaced are in `docs/agent-findings.md`.

```bash
# List the implemented metrics
agent-evals list-metrics

# Run a suite against a target (see src/agent_evals/config/targets.yaml).
# The `local` target mints an unsigned JWT for the backend's `local` profile.
agent-evals run --target local --suite hr --metrics primary --judge azure_openai --sink jsonl

# Deterministic/operational metrics only (no LLM judge needed):
agent-evals run --target local --suite hr --metrics deterministic

# Specific metrics, MLflow sink, custom suite file:
agent-evals run --target local --suite ./my_suite.yaml \
  --metrics tool_selection_accuracy,faithfulness,latency --sink mlflow

# Full judged run — all metrics + the LLM judge (the baseline command):
agent-evals run --target local --suite hr --metrics all --judge azure_openai --sink jsonl

# Ingest production user-feedback (#23) as an offline aggregate:
agent-evals ingest-feedback --input feedback.jsonl --sink jsonl
```

`--metrics` accepts `all`, `primary` (#1–15), `secondary` (#16–24), a family
(`deterministic`/`judge`/`operational`/`probe`), or a comma-separated list of
metric ids. Judge backends: `azure_openai` (default), `openai`, `mlflow`,
`deepeval`, `heuristic` (no LLM). Per-metric judge selection lives under `judge:`
in the config.

### Running against the backend

The backend's chat endpoint is AG-UI over SSE
(`POST /api/v1/bff/ai/agent/sse`). Point a target at it and run. The live
end-to-end smoke test exercises a real backend (auto-skipped otherwise):

```bash
AGENT_EVALS_LIVE_URL=http://localhost:8080/api/v1/bff/ai/agent/sse \
AGENT_EVALS_USER_LOGIN_ID=<your-real-login-id> pytest tests/test_live_smoke.py -v
```

**The agent serves only the caller's own profile** (resolved from the JWT user
login id), so use a **real login id that already has a profile** (e.g. your own) — a
fake/missing one makes the agent reply *"I can't find your profile."* The harness
creates the chat session automatically (GraphQL `createSession`) before the first
turn; if that call fails, the backend lazily creates a session for the run's
threadId, so the turn still proceeds.

For non-local environments, keep the no-SSO `local_jwt` auth (the minted token
carries `roles` + the `readwrite.api.bff` scope) pointed at the forwarded URL, or
set `auth.type: static` and supply a real bearer token via the configured env var.

**TLS (corporate / private-CA endpoints).** If the backend's certificate chains
to an internal CA, default verification fails (`CERTIFICATE_VERIFY_FAILED`). Fix
it without disabling security:
- *CLI*: add a `tls:` block to the target — `use_truststore: true` (uses the OS
  trust store / macOS Keychain; `pip install truststore`), or `ca_bundle: <path>`,
  or `insecure: true` (dev only).
- *Smoke test*: set `AGENT_EVALS_USE_TRUSTSTORE=1` (recommended), or
  `AGENT_EVALS_CA_BUNDLE=/path/to/ca.pem`, or `AGENT_EVALS_INSECURE=1`.

### Environment for a reproducible run

Pin everything a run depends on so anyone can reproduce it. Per-developer values
go in `.env` (auto-loaded); the proxy bypass must be **exported in the launching
shell** — the corp profile already exports `no_proxy` and the harness's
`load_dotenv()` will not override an already-set variable, so a `.env` edit to it
is ignored.

```bash
# .env — identity, endpoints, and judge credentials
AGENT_EVALS_USER_LOGIN_ID=<a real login id that already has a profile>
# base_url per target (the `local` target defaults to localhost; set the one you use):
AGENT_EVALS_DEVPOD_BASE_URL=https://<forwarded-host>/api/v1/bff/ai/agent/sse
AZURE_OPENAI_API_KEY=<key>
AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com/
AZURE_OPENAI_API_VERSION=2024-10-21
AZURE_OPENAI_DEPLOYMENT_NAME=<deployment>
SSL_CERT_FILE=/etc/ssl/certs/ca-bundle.crt          # corp CA bundle (or use a target `tls:` block)
```

```bash
# shell — bypass the proxy for the judge's host so its call goes direct, not via
# the public-blocking proxy (set the lowercase var; the corp profile presets it)
export no_proxy="${no_proxy},<judge-host>"; export NO_PROXY="$no_proxy"
```

Two things the eval can't set but you should **record** for reproducibility: the
**backend's own LLM credentials** (configured backend-side; without them the agent
itself errors), and the **backend build/commit under test**.

The backend's model goes into `params.json` (E19), **read from the backend itself
where possible**. The harness probes the target's Spring Boot actuator, before the
run and again after it:

- `/actuator/metrics/gen_ai.client.operation`, the OpenTelemetry GenAI meter Spring
  AI publishes, gives `model`, `response_model`, `provider` and the call count.
- `/actuator/configprops` (falling back to `/actuator/env`) gives `reasoning_effort`,
  `api_version` and `deployment`. These cannot come from the meter: a Micrometer
  meter carries only the convention's low-cardinality tags, and request options are
  not among them.

The meter is registered lazily, so a backend that has not yet called an LLM reports
nothing at run start and reports the model by the time the run ends. That is why it
is probed twice and `params.json` is rewritten afterwards.

**What the backend must expose.** Spring Boot serves only `health` over HTTP by
default, and a 404 on any of these shows up as an `error` in the run's `probe`
block. To get the full block:

```yaml
management:
  endpoints:
    web:
      exposure:
        include: health,info,metrics,configprops    # `env` also works as a fallback
  endpoint:
    configprops:
      show-values: ALWAYS      # Spring Boot 3 defaults to NEVER, which redacts
    env:                       # EVERY value to `******`, not just the secrets
      show-values: ALWAYS
```

`show-values` is the part that is easy to miss: without it the endpoint returns 200
and every value reads `******`, which the harness drops rather than record as a
model name. Note that `ALWAYS` also unredacts API keys to anyone who can reach the
endpoint, so on a shared deployment prefer exposing `configprops` only, or skip
this and declare the values instead.

**`backend_config`: what the backend's own `application.yaml` declares.** A
separate `params.json` section, read straight off disk, needing no `.env` entry
and no environment variable. When the eval runs beside the backend the file is
found by sweeping the working directory and its parent for a Spring config that
configures a chat model, so a service monorepo's other `application.yaml` files,
and the eval's own config, cannot be mistaken for it. It records `model`,
`reasoning_effort`, `deployment`, `temperature`, `timeout` and friends, plus the
`spring.ai` subtree they came from, which service (`application_name`) and which
files were read, each with a **sha256 digest**, so two runs can be shown to share
a configuration without trusting the values to have been copied correctly.

A **Spring Boot fat jar** is read the same way (`BOOT-INF/classes/`), and a built
jar **outranks** a source tree when both are found, with `kind` saying which
answered. The jar is the artefact the pod actually started; the checkout beside
it may have been synced or edited since, and this section exists precisely
because a deployment can stop matching its own source.

This is the only source that can answer **reasoning effort** on a deployment
whose actuator exposes just `health` and `metrics`: a Micrometer meter cannot
carry a request option, and `configprops`/`env` are frequently off.

It is deliberately **not** merged into `backend`. That block records what the
running process did; this one records what the build was configured to do, and
the two disagreeing is the finding rather than a conflict to resolve. Only the
*chat* model is read: a service that also configures embeddings has a second
`model` key, and recording `text-embedding-3-large` as the model that answered
the user would be simply wrong. Secrets are never recorded: a value under a key
like `api-key` becomes `<redacted>`, while a `${VAR}` placeholder is kept
verbatim because it names where the secret comes from without disclosing it, and
the backend's placeholders are never resolved against the eval's own
environment. Narrow or disable the search with an optional `model_config:` block
on the target (`path`, `profiles`, `enabled: false`).

**Declare what neither can reach.** Put a `model:` block on the target in
`targets.yaml` (its fields are `${AGENT_EVALS_*}`-backed, so they can live in
`.env`), or pass `--model` / `--deployment` / `--reasoning-effort` /
`--api-version` per run. Declared values fill in whatever the actuator did not
answer, so a backend with no actuator exposed behaves exactly as before.

Every field carries its own provenance in `field_source` (`observed` or
`declared`), summarised by `source` (`observed` / `declared` / `mixed` /
`unknown`), and the `probe` block records which endpoint answered and which
refused. **When a declaration disagrees with the running backend, the observed
value wins**, the declaration is kept as `declared_<field>` with a
`<field>_mismatch` flag, and the run prints a warning: a stale declaration is the
failure E19 exists to catch, so it must stay visible in the bundle. A run that
neither declared nor observed a model still runs, but warns and its artifacts
can't name the model that produced them.

The **judge** model needs no declaring. `judge: azure_openai` names a backend, not
a model, so the run also records `judge_model` (backend, model/deployment,
temperature, max_tokens, api_version) read off the judge the harness actually
built, plus `judge_per_metric` when a metric is bound to a different backend.
Credentials are never recorded. Change `AZURE_OPENAI_DEPLOYMENT_NAME` and the
next run's artifacts say so, which is what makes two judged runs comparable.

```bash
agent-evals run --target local --suite hr --metrics all --judge azure_openai \
  --model gpt-5.5 --reasoning-effort high
```

The TLS/proxy reasoning lives in `docs/troubleshooting.md`; the full
setup path is in `docs/evaluation-setup.md`.

### Viewing results

**JSONL sink** (default) writes to `eval-runs/<run-name>/`: `summary.json`
(aggregates), `scores.jsonl` (per-case scores), `runs.jsonl` (full
`RunRecord`s), `cases.jsonl`, `params.json` (what was run: suite, target,
metrics, judge, version, the `dataset` fingerprint, the `backend` model /
deployment / reasoning effort / API version with per-field provenance, and the
observed `judge_model`).

**MLflow sink** — install the extra, run with `--sink mlflow`, then launch the
UI on the same tracking store:

```bash
pip install -e ".[mlflow]"
agent-evals run --target local --suite hr --sink mlflow --experiment agent-evals
# data → ./mlruns by default; override with --tracking-uri sqlite:///mlflow.db
# or --tracking-uri http://<server> (or export MLFLOW_TRACKING_URI before both commands)

mlflow ui                                      # from the same dir → http://localhost:5000
mlflow ui --backend-store-uri sqlite:///mlflow.db   # if you used a custom store
```

In the UI, open the **`agent-evals`** experiment → your run (`<suite>-<target>-<timestamp>`):

- **Metrics** — run aggregates (`*.mean`, `*.pass_rate`, `latency.ttft_ms.p50/p95`,
  `latency.abort_rate`, `tokens.total.sum`, `tokens.estimated_fraction`) plus
  per-case series under each bare metric id (`step` = case index).
- **Parameters** — `suite`, `target`, `metrics`, `judge`, `sink`, `version`.
- **Artifacts** → `agent-evals/` — `summary.json`, `scores.jsonl`, `runs.jsonl`,
  `cases.jsonl` (the same bundle the JSONL sink writes, for drill-down).

> Without the backend running, every run errors out (`latency.abort_rate = 1.0`,
> most scorers skipped) — the MLflow plumbing works, but the numbers are not
> meaningful until the agent is reachable.

### Extending

- **New metric** → add a `Scorer` in `agent_evals/scorers/` and register it.
- **Another framework instead of MLflow** → add one `MetricsSink` in
  `agent_evals/sinks/` (and optionally a `Judge` in `agent_evals/judges/`).
  `core/`, `scorers/`, and `transport/` are untouched — that is the design's
  whole point. `JsonlSink` is the reference implementation.
- **Another wire protocol (e.g. A2A)** → add a `Transport`; it populates the
  same `RunRecord`, so every scorer keeps working (`transport/a2a/` is the
  worked example).

## Status

All in-scope metrics from `docs/metrics.md` are implemented (the Excluded
retrieval metrics are N/A — no retriever). The eval is **data-independent** — it
asserts behaviour, not specific records, so the same suite runs across
environments; data-dependent cases self-skip per run and are reported in the
summary. A calibrated reference run is kept locally as the frozen baseline.

**Scope and calibration (2026-08-17).** The suite covers the **employee** persona:
profile and skills, role discovery and Q&A, recruiter outreach. Hiring-manager
capabilities are deliberately out of scope for this release, so three of the
backend's agent prompts have no cases. The suite is calibrated mechanically, but
parts of it still encode a capability model the product has moved past; the open
work is tracked in
[`docs/issues/EVAL-FIX-BACKLOG.md`](docs/issues/EVAL-FIX-BACKLOG.md) and sequenced
in [`docs/issues/EVAL-FIX-PLAN.md`](docs/issues/EVAL-FIX-PLAN.md). Scorer and
runner fixes can be validated without a live agent by replaying frozen runs with
`agent-evals rescore`.

The test suite runs fully offline (the transport is verified against a mock
backend reproducing the exact AG-UI/SSE wire contract); the live smoke test runs
against a real backend when `AGENT_EVALS_LIVE_URL` is set.
