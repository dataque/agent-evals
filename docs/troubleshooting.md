# Troubleshooting live runs

How to read a run that "worked" but is missing numbers, and how to fix the
common failures when pointing the harness at a real backend. The single most
useful habit: **when something looks off, read `scores.jsonl`** — it records the
real per-case reason that the summary deliberately hides (see below).

## Some metrics are missing from the summary

A run with `--metrics primary` (or `all`) prints the deterministic, probe, and
operational metrics but **silently omits the 9 LLM-judge metrics**:

`task_completion`, `faithfulness`, `answer_equivalence`, `safety`,
`refusal_correctness`, `conversation_completeness`, `knowledge_retention`,
`topic_adherence`, `bias`.

If those are absent while `tool_selection_accuracy`, `tool_argument_correctness`,
`tool_result_schema_adherence`, `cross_user_isolation`, `latency.*` and
`tokens.*` are present, the judge backend is **failing on every call** — it is
not a scoring bug.

### Why the failure is silent

The judge swallows its own exceptions and the aggregator drops non-real scores,
so a judge outage never reaches the console:

- `judges/base_openai.py` — `evaluate()` catches any exception and returns an
  *error verdict*; it never raises.
- `scorers/_judge_base.py` — `judged()` turns an error verdict into
  `Score.failed(...)` (sets `error`, leaves `value=None`).
- `core/runner.py` — `_aggregate()` skips any score where `skipped` is true or
  `error is not None`, so it never lands in `summary.json`. Dropping (rather than
  scoring `0.0`) is deliberate: a judge outage must not masquerade as real
  zero-quality.
- Because nothing raised, the runner's per-scorer `except` never logs.

### See the real reason — read `scores.jsonl`

The exception is recorded per case, just not surfaced. Group the distinct
errors/skips for a run:

```bash
python3 - <<'PY'
import json, collections
c = collections.Counter()
for line in open("eval-runs/<run-dir>/scores.jsonl"):
    s = json.loads(line)
    if s.get("error"):     c[(s["metric"], s["error"][:160])] += 1
    elif s.get("skipped"): c[(s["metric"], "SKIP: " + (s.get("skip_reason") or ""))] += 1
for (metric, reason), n in c.most_common():
    print(f"{n:3d}  {metric:28s} {reason}")
PY
```

`SKIP: empty assistant text` (or `no tool outputs to ground against`) is benign —
the case had nothing to judge. An `error:` line is the judge backend failing;
the most common one is the Azure 403 below.

## Azure judge: `403 Public access is disabled. Please configure private endpoint`

This is the Azure resource's **network policy**, not your credentials. The
resource has public network access disabled and only accepts requests over its
approved Private Link / VNet path. Azure rejects the request at the **network
layer, before the API key is read** — so:

- It is **not** a bad key, a wrong `AZURE_OPENAI_DEPLOYMENT_NAME`, or a wrong
  `AZURE_OPENAI_API_VERSION`. All four `AZURE_OPENAI_*` values can be correct and
  the deployment healthy.
- Switching api-key ↔ Entra/AAD auth makes no difference; the policy is evaluated
  before any credential.

### Why the backend reaches the model but the judge doesn't

Your turns completed (`latency.abort_rate` near 0), so the **system-under-test
reaches the Azure model from the dev pod**. The judge calls the **same** Azure
resource from a **different process**, so a 403 means the eval's process is
egressing differently from the backend — typically one of:

- a corporate **HTTPS proxy** the backend uses (the approved egress) that the
  eval's shell hasn't set, so the eval goes out the default route to the public IP;
- **private DNS** that resolves the host to a `10.x` address for the backend but
  not for the eval's resolver.

Same machine ≠ same egress once a proxy or private-DNS override is involved.

### Diagnose

```bash
# 1. Reproduce at the network layer (bypasses the harness entirely):
curl -sS -o /dev/null -w '%{http_code}\n' \
  -H "api-key: $AZURE_OPENAI_API_KEY" \
  "$AZURE_OPENAI_ENDPOINT/openai/v1/models"
#   403 here  -> network policy confirmed
#   public IP from `getent hosts <resource>.openai.azure.com` -> DNS/egress is the issue

# 2. Find the egress the backend uses (proxy is the usual answer):
env | grep -iE 'proxy'
#   if the backend runs as a local process, inspect ITS environment:
#   tr '\0' '\n' < /proc/<backend-pid>/environ | grep -iE 'AZURE_OPENAI|PROXY'
```

### Fix — match the backend's egress

The judge uses the OpenAI SDK over httpx with `trust_env=True`, so it honors
proxy environment variables automatically — **no code change needed**:

```bash
export HTTPS_PROXY=http://<the-backend-proxy>:<port>
export HTTP_PROXY=$HTTPS_PROXY
# CRITICAL: keep the backend's own URL DIRECT or your SSE turns start failing.
# Only the Azure OpenAI call should traverse the proxy:
export NO_PROXY=<backend-host>,localhost,127.0.0.1,.svc,.cluster.local

agent-evals run --target devpod --suite hr --metrics primary --judge azure_openai --sink jsonl
```

If there is no proxy (the backend reaches Azure via private DNS directly), run
the eval from the same network context the backend uses, or set
`AZURE_OPENAI_ENDPOINT` to the private-endpoint FQDN.

### Keep moving while you sort the network out

Validate the full scoring pipeline end-to-end with the offline judge — it emits
real `Score`s for all 9 judge metrics (no LLM, so the quality numbers are a
degraded stand-in, but it proves the metrics flow through and aggregate):

```bash
agent-evals run --target devpod --suite hr --metrics primary --judge heuristic --sink jsonl
```

## Other Azure judge errors (from `scores.jsonl`)

| `error:` contains | Cause | Fix |
|---|---|---|
| `No module named 'openai'` | judge SDK not installed | `pip install -e ".[openai]"` |
| `401` / `invalid api key` | wrong/empty key | check `AZURE_OPENAI_API_KEY` |
| `404` / `DeploymentNotFound` | `AZURE_OPENAI_DEPLOYMENT_NAME` is the model name, not the deployment, or wrong `AZURE_OPENAI_API_VERSION` | use the deployment name; align the api-version |
| `400` re `max_tokens` / `temperature` | some GPT-5-family deployments require `max_completion_tokens` and a fixed temperature | use a chat deployment that accepts the classic params, or adjust the judge call |

## Common backend-transport errors

These show up as `RUN_ERROR` or a high `latency.abort_rate`, visible in the
run records (`runs.jsonl`, field `error`).

| Symptom | Cause | Fix |
|---|---|---|
| `CERTIFICATE_VERIFY_FAILED` | endpoint chains to an internal CA | target `tls.use_truststore: true` (or `ca_bundle`, or `insecure` for dev) |
| `RUN_ERROR: "No value present"` | backend loads the chat thread and it's missing | leave `create_thread: true` (default) so the harness creates it first |
| `RUN_ERROR: "Stream processing failed"` | the **backend's own** LLM credentials aren't set (its Azure key) | set the backend's LLM key — this is a backend config issue, not an eval one |
| `"I can't find your profile"` / `TALENT_MARKETPLACE_PROFILE_NOT_FOUND` | the GPN has no profile | the agent serves only the caller's own profile — use a **real GPN that already has one** |
| every run errors, `abort_rate = 1.0` | backend unreachable, wrong `base_url`, or missing `readwrite.api.bff` scope | verify the URL is reachable and the token carries the required scope/roles |

## Reading a run's outputs

`eval-runs/<run-name>/`:

- `summary.json` — aggregates. **Failed/skipped scores are excluded**, so a
  metric missing here is the signal to open `scores.jsonl`.
- `scores.jsonl` — one row per scorer invocation, including `error`,
  `skipped`, `skip_reason`, `rationale`. **The diagnostic source of truth.**
- `runs.jsonl` — the full `RunRecord` per turn (events, timing, `error`).
- `cases.jsonl`, `params.json` — the cases scored and the run parameters.
