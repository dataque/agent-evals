# agent-evals

A generic, **framework-independent** evaluation system for **agentic chat systems**.

It drives an agent over its live wire protocol, normalizes every run into a
transport-neutral **`RunRecord`**, and scores it against the metric catalog in
[`docs/metrics.md`](docs/metrics.md). MLflow is the first metrics backend, but
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
| `agent_evals.datasets` | Eval suites + loader. |

## Install

```bash
pip install -e ".[dev]"          # core + tests
pip install -e ".[mlflow]"       # + MLflow sink/judge
pip install -e ".[deepeval]"     # + DeepEval judge
pip install -e ".[openai]"       # + Azure/OpenAI judge (default)
```

## Usage

Per-developer values (your GPN, tokens, judge keys) go in a gitignored **`.env`**
(copy `.env.example`). The CLI auto-loads it, and `targets.yaml` references vars as
`${VAR}` — e.g. `gpn: "${AGENT_EVALS_GPN}"`. So set `AGENT_EVALS_GPN=<your real GPN>`
in `.env` before running.

```bash
# List the 24 implemented metrics
agent-evals list-metrics

# Run a suite against a target (see src/agent_evals/config/targets.yaml).
# The `local` target mints an unsigned JWT for the backend's `local` profile.
agent-evals run --target local --suite hr --metrics primary --judge azure_openai --sink jsonl

# Deterministic/operational metrics only (no LLM judge needed):
agent-evals run --target local --suite hr --metrics deterministic

# Specific metrics, MLflow sink, custom suite file:
agent-evals run --target local --suite ./my_suite.yaml \
  --metrics tool_selection_accuracy,faithfulness,latency --sink mlflow

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
AGENT_EVALS_GPN=TEST0001 pytest tests/test_live_smoke.py -v
```

For non-local environments, set `auth.type: static` in the target and provide a
real bearer token via the configured env var.

**TLS (corporate / private-CA endpoints).** If the backend's certificate chains
to an internal CA, default verification fails (`CERTIFICATE_VERIFY_FAILED`). Fix
it without disabling security:
- *CLI*: add a `tls:` block to the target — `use_truststore: true` (uses the OS
  trust store / macOS Keychain; `pip install truststore`), or `ca_bundle: <path>`,
  or `insecure: true` (dev only).
- *Smoke test*: set `AGENT_EVALS_USE_TRUSTSTORE=1` (recommended), or
  `AGENT_EVALS_CA_BUNDLE=/path/to/ca.pem`, or `AGENT_EVALS_INSECURE=1`.

### Viewing results

**JSONL sink** (default) writes to `eval-runs/<run-name>/`: `summary.json`
(aggregates), `scores.jsonl` (per-case scores), `runs.jsonl` (full
`RunRecord`s), `cases.jsonl`, `params.json`.

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

All 24 in-scope metrics from `docs/metrics.md` are implemented (Level-3 retrieval
metrics are N/A — no retriever). Test suite runs fully offline (the transport is
verified against a mock backend reproducing the exact AG-UI/SSE wire contract);
the live smoke test runs against a real backend when `AGENT_EVALS_LIVE_URL` is set.
