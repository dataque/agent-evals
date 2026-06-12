# chat-evals

An MLflow-driven evaluation harness for any agent that speaks the **A2A
JSON-RPC protocol**. It sends questions to a reachable A2A endpoint via
`message/send`, captures the responses (text, tool-call trace, artifacts, and
task metadata), and scores them with MLflow GenAI scorers plus a set of custom
trace-aware scorers.

The agent under test is **not** run in-process — the harness only needs a
reachable A2A endpoint. It is domain-agnostic: bring your own datasets,
endpoints, and (optionally) scoring rubrics.

---

## Quickstart

```bash
# 1. Install
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# edit .env            → add OPENAI_API_KEY (or AZURE_OPENAI_* for Azure)
# edit evals/targets.yaml → point a target at your A2A endpoint

# 3. Smoke-test the endpoint (no scoring, no MLflow)
A2A_BASE_URL="https://your-endpoint/" python evals/test_a2a.py "What can you help me with?"

# 4. Run a benchmark
python -m evals --mode local --agent example

# 5. View results
mlflow ui
# open http://127.0.0.1:5000
```

Python 3.11+ recommended.

---

## Configuration

### `.env` — judge LLM credentials

The built-in MLflow scorers in `--mode local` use an LLM as judge. Configure
**one** provider in `.env`:

| Provider | Variables | Notes |
|---|---|---|
| OpenAI | `OPENAI_API_KEY` | Judge defaults to `openai:/gpt-4o` |
| Azure OpenAI | `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT_NAME`, `AZURE_OPENAI_API_VERSION` | Judge defaults to `azure:/<deployment>` |

Override the judge model explicitly with `JUDGE_MODEL` (e.g. `openai:/gpt-4o-mini`).
`evals/run.py` walks up from the package directory looking for `.env`, so placing
it at the repo root works automatically.

### `evals/targets.yaml` — A2A endpoint registry

Named targets selectable via `--target`:

```yaml
direct:
  url: "https://your-agent-endpoint.example.com/a2a?code=YOUR_CODE_HERE"
  description: "Direct A2A endpoint (auth via a key embedded in the URL, or none)"
  requires_token: false

remote-dev:
  url: "https://your-remote-service.example.com/api/v1/agent/a2a"
  description: "Remote service requiring a bearer token (mints a thread via GraphQL)"
  requires_token: true
```

To add a target, append a block with `url`, `description`, and `requires_token`
— no code change needed. Token-protected targets require `--token <token>` at
runtime (sent as a `Bearer` header); they also mint a conversation thread via a
GraphQL `createThread` mutation before sending.

### Agent metadata namespace

Trace-aware scorers and the performance metadata helpers read task-metadata keys
under a namespace prefix (default `agent`, e.g. `agent.latency_ms`). If your
agent emits a different prefix, set `A2A_METADATA_NS` in `.env`.

---

## Running benchmarks

### Modes

| `--mode` | Benchmarker | When to use |
|---|---|---|
| `local` (default) | `LocalBenchmarker` (local MLflow) | Day-to-day dev. Supports multi-turn datasets via a shared `contextId`. |
| `aice` | `AICEBenchmarker` from the optional `aice-benchmarker` package | External benchmarker integration. Requires uncommenting `aice-benchmarker` in `requirements.txt`. Multi-turn items are flattened to single-turn. |

### Examples

```bash
# Local MLflow eval against the default target
python -m evals --mode local --agent example

# Token-protected remote target
python -m evals --target remote-dev --mode local --agent example --token <token>

# Override the URL directly (ignores --target)
python -m evals --base-url "https://my-endpoint/..." --mode local --agent example

# All registered datasets, multiple trials for variance
python -m evals --mode local --n-trials 3

# Hyperparameter grid (combinatorial)
python -m evals --mode local --agent example \
  --hyperparameters '{"model": ["gpt-4o", "gpt-4o-mini"], "temperature": [0.3, 0.7]}'
```

### CLI reference

| Flag | Default | Description |
|---|---|---|
| `--target` | `direct` | Named target from `targets.yaml` |
| `--token` | _none_ | Bearer token for token-protected targets |
| `--mode` | `local` | `local` or `aice` |
| `--agent` | _all_ | Dataset to run (key from `ALL_DATASETS`) |
| `--n-trials` | `1` | Trials per sample (variance) |
| `--scorers` | `all` | `all`, `builtin`, or `custom` |
| `--experiment-name` | `a2a-agent-benchmark-<target>` | MLflow experiment name |
| `--base-url` | _none_ | Override target URL |
| `--hyperparameters` | _none_ | JSON grid for combinatorial eval |
| `-v`, `--verbose` | off | Debug logging |

---

## Datasets

Defined in `evals/datasets.py`. Each item is single-turn or multi-turn:

```python
# Single-turn
{"inputs": {"question": "..."}, "expectations": {...}}

# Multi-turn — turns share a contextId in --mode local
{"inputs": {"scenario": "...", "turns": [
    {"question": "...", "expectations": {...}},
    ...
]}}
```

`expectations` fields are all optional; each scorer skips when its field is
absent:

| Field | Used by |
|---|---|
| `expected_response` | `Correctness` (LLM judge) |
| `response_must_contain` | `response_completeness` |
| `expected_tool_calls` | `tool_trace_f1` |
| `expected_tool_args` | `tool_argument_correctness` |
| `max_steps` | `step_efficiency` |
| `expected_routes`, `allowed_tool_calls` | `plan_quality` |
| `expected_actions` | `audit_log_action_taken` |
| `expected_artifacts` | `artifact_format_correctness` |

Register your datasets by adding them to the `ALL_DATASETS` dict in
`datasets.py`; the key becomes a valid `--agent` value. A neutral `example`
dataset ships by default.

## Scorers

Defined in `evals/scorers.py`:

- **Built-in MLflow scorers** (LLM-judged):
  - `Correctness` — does the response match `expectations.expected_response`
  - `RelevanceToQuery` — is the response on-topic for the question
  - `Safety` — no PII leakage, no harmful content
  - `Guidelines` — one per rubric. Defaults to a domain-neutral set
    (`professional_tone`, `no_sensitive_data`); pass your own `guidelines`
    dict to `get_builtin_scorers` / `get_all_scorers` for domain-specific rules.
- **Custom text scorer**: `response_completeness` — fraction of
  `response_must_contain` strings found in the output.
- **Trace-aware scorers** (consume the `execution_trace` artifact): `tool_trace_f1`,
  `tool_argument_correctness`, `step_efficiency`, `plan_quality`,
  `audit_log_action_taken`.
- **Artifact-aware scorer**: `artifact_format_correctness`.

Select with `--scorers all|builtin|custom`.

---

## Smoke testing

Before a full benchmark, verify the endpoint is reachable:

```bash
A2A_BASE_URL="https://your-endpoint/" \
A2A_FUNC_KEY="your-function-key" \
python evals/test_a2a.py "What can you help me with?"
```

This sends a single A2A `message/send` JSON-RPC request and pretty-prints the
response. No scoring, no MLflow.

---

## Viewing results

Each benchmark run logs to MLflow:

```bash
mlflow ui
# open http://127.0.0.1:5000
```

Or use the helper `bash evals/mlflow_ui.sh`. Runs are organized into experiments
named `a2a-agent-benchmark-<target>-<agent>` (override with `--experiment-name`).
Each sample becomes a row with the question, response, expectations, and
per-scorer metrics.

---

## Trace-aware scoring

Trace and artifact scorers require the agent to return a structured A2A response:
an `execution_trace` artifact (a list of `route` / `tool_call` / `tool_result`
events) and any named result artifacts. The expected shape is documented in
`evals/schemas/a2a_response.v1.json`. Agents that return text only still work —
the trace/artifact scorers simply return `None` (skipped) for those rows.

---

## Repo layout

```
evals/
├── __main__.py          # python -m evals entry point
├── run.py               # CLI + run_benchmark()
├── datasets.py          # example datasets (single- and multi-turn)
├── scorers.py           # MLflow scorers (built-in + custom)
├── targets.yaml         # named A2A endpoint registry
├── test_a2a.py          # raw A2A smoke test
├── mlflow_ui.sh         # convenience launcher for the MLflow UI
├── schemas/
│   └── a2a_response.v1.json   # expected structured-response shape
├── benchmarker/         # local MLflow runner + A2A HTTP client
│   ├── a2a_client.py    #   make_a2a_predict_fn, create_graphql_thread
│   └── benchmarker.py   #   LocalBenchmarker class
└── tests/               # unit tests for the client + trace scorers
```
