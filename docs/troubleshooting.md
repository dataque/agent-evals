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

### `Connection error` instead of `403` — the TLS/truststore coupling

If `scores.jsonl` shows `Connection error` (the OpenAI SDK's `APIConnectionError`)
rather than `403`, the SDK couldn't open or verify the socket at all — and it
flattens the real cause to that bare string, so `scores.jsonl` won't say which.
The usual cause is the **corporate-CA TLS verification failing for the Azure
host** (certifi has no corp root), and it is coupled to the *target* you ran,
because `truststore.inject_into_ssl()` is a process-wide monkeypatch:

- a target with `tls.use_truststore: true` (e.g. `devpod`) makes the judge verify
  against the **OS trust store** → verifies *if* that store has the corp CA, then
  the `403` surfaces;
- a target with no `tls` block (e.g. `local`) leaves the judge on **certifi**,
  which lacks the corp CA → TLS verify fails → `Connection error`.

Surface the real cause (run in the pod, with `.env` loaded). It makes one call on
each path, so it also shows the `403` wall sitting behind the TLS layer:

```python
python3 - <<'PY'
import os
from agent_evals.envfile import load_dotenv; load_dotenv()
from openai import AzureOpenAI

def call(label):
    c = AzureOpenAI(api_key=os.environ["AZURE_OPENAI_API_KEY"],
                    azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
                    api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21"))
    try:
        c.chat.completions.create(model=os.environ["AZURE_OPENAI_DEPLOYMENT_NAME"],
                                  messages=[{"role": "user", "content": "ping"}], max_tokens=5)
        print(label, "OK")
    except Exception as e:
        print(label, "FAIL:", type(e).__name__, "->", e)
        cause = e.__cause__
        while cause:
            print("   cause:", type(cause).__name__, "->", cause)
            cause = cause.__cause__

call("certifi  (mirrors --target local):")
import truststore; truststore.inject_into_ssl()
call("OS store (mirrors --target devpod):")
PY
```

Two outcomes are common:

- certifi line → `SSLCertVerificationError … unable to get local issuer certificate`,
  OS-store line → Azure `403 … Public access is disabled`: the OS store trusts the
  chain; fix the `403` via the egress steps below.
- **both** lines → `CERTIFICATE_VERIFY_FAILED … unable to get local issuer
  certificate`: the cert you're handed is signed by a CA in **neither** store — a
  **corporate TLS-intercepting proxy** (or a private-CA endpoint) sits in the
  judge's path. You aren't reaching Azure at all; you need *that* CA, which the
  backend already trusts (that's how it connects).

When **both** fail, identify the intercepting CA and the bundle that already trusts it:

```bash
H=<your-azure-openai-host>   # host from AZURE_OPENAI_ENDPOINT, e.g. <resource>.openai.azure.com

python3 -c "import httpx; print(httpx.get('https://example.com').status_code)"   # 200 => certifi fine; only this host is intercepted
openssl s_client -connect "$H:443" -servername "$H" </dev/null 2>/dev/null | openssl x509 -noout -issuer   # a corp issuer => interception
curl -sS -o /dev/null -w '%{http_code}\n' "https://$H/"            # does curl trust it without -k?
curl -v "https://$H/" 2>&1 | grep -iE 'CAfile|CApath'             # the bundle curl uses
env | grep -iE 'CA_BUNDLE|SSL_CERT|CA_CERT|REQUESTS_CA|NODE_EXTRA' # an already-exported corp bundle?
```

Then point Python at that bundle — the OpenAI SDK / httpx honor `SSL_CERT_FILE`, so
this is target-independent and needs **no code change**:

```bash
export SSL_CERT_FILE=<the CAfile curl uses, or the corp bundle from env>
# common: /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem  or  /etc/ssl/certs/ca-certificates.crt
```

If curl also fails and no bundle env var exists, extract the CAs the backend's JVM
already trusts and use those:

```bash
keytool -list -rfc -keystore "$JAVA_HOME/lib/security/cacerts" -storepass changeit > /tmp/corp-cas.pem
export SSL_CERT_FILE=/tmp/corp-cas.pem
```

Once TLS passes you'll either succeed, or finally see the `403` — the real
blocker, fixed via the egress / private-endpoint steps below.

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
| `Connection error` | SDK couldn't open/verify the socket — usually corp-CA TLS verify failure (certifi lacks the CA), or no egress to Azure | set `SSL_CERT_FILE` to the corp bundle (see "`Connection error` instead of `403`" above), then resolve the egress |

## Common backend-transport errors

These show up as `RUN_ERROR` or a high `latency.abort_rate`, visible in the
run records (`runs.jsonl`, field `error`).

| Symptom | Cause | Fix |
|---|---|---|
| `CERTIFICATE_VERIFY_FAILED` | endpoint chains to an internal CA | target `tls.use_truststore: true` (or `ca_bundle`, or `insecure` for dev) |
| `RUN_ERROR: "No value present"` | backend loads the chat thread and it's missing | leave `create_thread: true` (default) so the harness creates it first |
| `RUN_ERROR: "Stream processing failed"` | the **backend's own** LLM credentials aren't set (its Azure key) | set the backend's LLM key — this is a backend config issue, not an eval one |
| `"I can't find your profile"` / `TALENT_MARKETPLACE_PROFILE_NOT_FOUND` | the login id has no profile | the agent serves only the caller's own profile — use a **real user login id that already has one** |
| every run errors, `abort_rate = 1.0` | backend unreachable, wrong `base_url`, or missing `readwrite.api.bff` scope | verify the URL is reachable and the token carries the required scope/roles |

## Reading a run's outputs

`eval-runs/<run-name>/`:

- `summary.json` — aggregates. **Failed/skipped scores are excluded**, so a
  metric missing here is the signal to open `scores.jsonl`.
- `scores.jsonl` — one row per scorer invocation, including `error`,
  `skipped`, `skip_reason`, `rationale`. **The diagnostic source of truth.**
- `runs.jsonl` — the full `RunRecord` per turn (events, timing, `error`).
- `cases.jsonl`, `params.json` — the cases scored and the run parameters.
