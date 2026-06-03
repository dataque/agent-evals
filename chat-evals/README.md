# chat-evals

MLflow-driven evaluation harness for an HR Agent A2A endpoint. Sends questions over the A2A JSON-RPC protocol (`message/send`), captures responses, and scores them with MLflow GenAI scorers (Correctness, RelevanceToQuery, Safety, Guidelines, plus a custom `response_completeness`).

The agent itself is not run in-process — this harness only needs a reachable A2A endpoint.

---

## Quickstart

```bash
# 1. Install
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# edit .env  → fill in AZURE_OPENAI_API_KEY / AZURE_OPENAI_ENDPOINT
# edit evals/targets.yaml → replace the placeholder fa.url with your real Function App URL

# 3. Smoke-test the endpoint (no scoring, no MLflow)
python evals/test_a2a.py "Analyse my profile"

# 4. Run a real benchmark
python -m evals --mode hr --agent profile

# 5. View results
mlflow ui
# open http://127.0.0.1:5000
```

Python 3.11+ recommended.

---

## Configuration

### `.env` — Azure OpenAI judge credentials

The MLflow scorers in `--mode hr` use Azure OpenAI as their judge LLM. Copy `.env.example` → `.env` and fill in:

| Variable | Required by | Purpose |
|---|---|---|
| `AZURE_OPENAI_API_KEY` | `--mode hr` | Judge LLM API key |
| `AZURE_OPENAI_ENDPOINT` | `--mode hr` | Judge LLM endpoint |
| `AZURE_OPENAI_DEPLOYMENT_NAME` | `--mode hr` | Defaults to `gpt-4o` |
| `AZURE_OPENAI_API_VERSION` | `--mode hr` | Defaults to `2024-12-01-preview` |
| `A2A_BASE_URL` | `evals/test_a2a.py` only | Smoke-test endpoint URL |
| `A2A_FUNC_KEY` | `evals/test_a2a.py` only | Function key for FA endpoints |

`evals/run.py` walks up from the package directory looking for `.env`, so placing it at the repo root works automatically.

### `evals/targets.yaml` — A2A endpoint registry

Named targets selectable via `--target`:

```yaml
fa:
  url: "https://your-a2a-function-app.azurewebsites.net/?code=YOUR_CODE_HERE"
  description: "Azure Function App (direct A2A)"
  requires_token: false

bff-dev:
  url: "https://cirruspl-gf-hr-tm-as-bff-svc-dev.azurewebsites.net/api/v1/bff/ai/agent/a2a"
  description: "BFF Dev environment"
  requires_token: true
```

Replace the placeholder `fa.url` with your real Function App URL (the `?code=` query param embeds the function key). BFF targets require an SSO Bearer token at runtime via `--token`.

To add a new target, append a block with `url`, `description`, and `requires_token`. No code change needed — `--target <new-name>` will pick it up.

---

## Running benchmarks

### Modes

| `--mode` | Benchmarker | When to use |
|---|---|---|
| `hr` (recommended) | `HRBenchmarker` (local MLflow) | Day-to-day dev. Supports multi-turn datasets via shared `contextId`. |
| `aice` (default) | `AICEBenchmarker` from the `aice-benchmarker` package | Official Databricks eval. Requires uncommenting `aice-benchmarker` in `requirements.txt`. Multi-turn items are flattened to single-turn. |

### Targets

| `--target` | URL source | Auth |
|---|---|---|
| `fa` (default) | `targets.yaml.fa.url` | Function key embedded in URL |
| `bff-dev` | `targets.yaml.bff-dev.url` | `--token <sso-token>` (Bearer header) |
| _custom_ | `--base-url "https://..."` overrides `targets.yaml` | None unless `--token` is also passed |

### Examples

```bash
# Default: AICE mode against the FA target — official benchmarker
python -m evals --agent profile

# Local MLflow dev eval against FA
python -m evals --mode hr --agent profile

# BFF dev environment with SSO token
python -m evals --target bff-dev --mode hr --agent profile --token <sso-token>

# Override the URL directly (ignores --target)
python -m evals --base-url "https://my-endpoint/..." --mode hr --agent profile

# All registered agents, multiple trials for variance
python -m evals --mode hr --n-trials 3

# Hyperparameter grid (combinatorial)
python -m evals --mode hr --agent profile \
  --hyperparameters '{"model": ["gpt-4o", "gpt-4o-mini"], "temperature": [0.3, 0.7]}'

# Verbose logging
python -m evals --mode hr --agent profile -v
```

### CLI reference

| Flag | Default | Description |
|---|---|---|
| `--target` | `fa` | Named target from `targets.yaml` |
| `--token` | _none_ | Bearer token for BFF targets |
| `--mode` | `aice` | `aice` or `hr` |
| `--agent` | _all_ | Dataset to run — currently `profile` |
| `--n-trials` | `1` | Trials per sample (variance) |
| `--scorers` | `all` | `all`, `builtin`, or `custom` |
| `--experiment-name` | `a2a-hr-agent-benchmark-<target>` | MLflow experiment name |
| `--base-url` | _none_ | Override target URL |
| `--hyperparameters` | _none_ | JSON grid for combinatorial eval |
| `-v`, `--verbose` | off | Debug logging |

### Datasets

Defined in `evals/datasets.py`. Each item is single-turn or multi-turn:

```python
# Single-turn
{"inputs": {"question": "..."}, "expectations": {...}}

# Multi-turn — turns share contextId in --mode hr
{"inputs": {"scenario": "...", "turns": [
    {"question": "...", "expectations": {...}},
    ...
]}}
```

Currently registered: `profile` (`PROFILE_SKILLS_DATASET`). Add new datasets by defining them in `datasets.py` and adding to the `ALL_DATASETS` dict.

### Scorers

Defined in `evals/scorers.py`:

- **Built-in MLflow scorers** (judge LLM = Azure OpenAI in `--mode hr`):
  - `Correctness` — does the response match `expectations.expected_response`
  - `RelevanceToQuery` — is the response on-topic for the question
  - `Safety` — no PII leakage, no harmful content
  - `Guidelines` (×3) — `professional_tone`, `hr_relevance`, `data_privacy`
- **Custom**: `response_completeness` — fraction of `expectations.response_must_contain` strings found in the output

Select with `--scorers all|builtin|custom`.

---

## Smoke testing

Before running a full benchmark, verify the endpoint is reachable:

```bash
A2A_BASE_URL="https://your-function-app.azurewebsites.net/" \
A2A_FUNC_KEY="your-function-key" \
python evals/test_a2a.py "Analyse my profile"
```

This sends a single A2A `message/send` JSON-RPC request and pretty-prints the response. No scoring, no MLflow.

---

## Viewing results

Each benchmark run logs to MLflow. View locally:

```bash
mlflow ui
# open http://127.0.0.1:5000
```

Or use the helper:

```bash
bash evals/mlflow_ui.sh
```

Runs are organized into experiments named `a2a-hr-agent-benchmark-<target>-<agent>` (override with `--experiment-name`). Each sample becomes a row with the question, response, expectations, and per-scorer metrics.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `ModuleNotFoundError: pysqlite3` | `pysqlite3-binary` not installed | `pip install -r requirements.txt` (or run from a venv that has it) |
| `Target 'bff-dev' requires a Bearer token` | `--target bff-dev` without `--token` | Pass `--token <sso-token>` |
| `Invalid --hyperparameters JSON` | Mis-quoted JSON | Use single quotes outside, double quotes inside: `--hyperparameters '{"key": [1, 2]}'` |
| `Unknown agent 'X'` | Agent not in `ALL_DATASETS` | Use `python -m evals --help` to list valid agents, or add the dataset in `datasets.py` |
| Scorers fail with auth errors | `.env` not loaded or wrong creds | Confirm `.env` is at repo root and contains valid Azure OpenAI credentials |
| `aice` mode import error | `aice-benchmarker` not installed | Uncomment it in `requirements.txt` and `pip install -r requirements.txt` |
| `evals.old_eval` fails to import | Expected — it depends on the original `hr-agent` package | Use `evals.run` instead (the supported A2A flow) |

---

## Repo layout

```
evals/
├── __main__.py              # python -m evals entry point
├── run.py                   # CLI + run_benchmark()
├── datasets.py              # PROFILE_SKILLS_DATASET (single- and multi-turn items)
├── scorers.py               # MLflow scorers (built-in + custom)
├── targets.yaml             # named A2A endpoint registry
├── test_a2a.py              # raw A2A smoke test
├── mlflow_ui.sh             # convenience launcher for the MLflow UI
├── hr_benchmarker/          # local MLflow runner + A2A HTTP client
│   ├── a2a_client.py        #   make_a2a_predict_fn, create_bff_thread
│   └── benchmarker.py       #   HRBenchmarker class
├── old_eval/                # legacy in-process runner — see note below
├── eval_metrics.md          # design doc — metrics
├── eval_scenarios.md        # design doc — scenarios
└── mlflow_metrics_feasibility.md   # design doc — feasibility study
```

## Note on `old_eval/`

`evals/old_eval/` is preserved for historical reference. It loads agents in-process via `core.state` and `agents.catalog`, which only exist inside the original `hr-agent` repo, so `python -m evals.old_eval.run` will fail with `ModuleNotFoundError` here. The current A2A flow under `evals/run.py` does not use it.

## Provenance

Extracted from the `hr-agent` repo (`hr-agent/evals/`). Source files copied verbatim — only the surrounding repo wrapper (this README, `requirements.txt`, `.env.example`, `.gitignore`) is new.
