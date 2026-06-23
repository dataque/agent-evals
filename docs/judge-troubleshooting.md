# Troubleshooting the LLM judge (missing metrics / Azure 403)

A run with `--metrics primary` (or `all`) shows the deterministic and operational
metrics but **silently drops the 9 LLM-judge metrics** (`task_completion`,
`faithfulness`, `answer_equivalence`, `safety`, `refusal_correctness`,
`conversation_completeness`, `knowledge_retention`, `topic_adherence`, `bias`).
This is almost always the judge failing to reach its model — not a scoring bug.

## 1. Why the failure is silent

The judge swallows its own exceptions and the aggregator drops non-real scores,
so a judge outage never reaches the console:

- `judges/base_openai.py` / `judges/langchain_azure.py` — `evaluate()` catches any
  exception and returns an *error verdict* (it never raises).
- `scorers/_judge_base.py` `judged()` turns an error verdict into
  `Score.failed(...)` (sets `error`, leaves `value=None`).
- `core/runner.py` `_aggregate()` skips any score where `skipped` is true or
  `error is not None`, so it never lands in `summary.json`.
- Because nothing raised, the runner's per-scorer `except` never logs.

**The real error is recorded per case in `scores.jsonl`** — just not surfaced.
Read it:

```bash
python3 - <<'PY'
import json, collections
c = collections.Counter()
for l in open("eval-runs/<run-dir>/scores.jsonl"):
    s = json.loads(l)
    if s.get("error"):      c[(s["metric"], s["error"][:160])] += 1
    elif s.get("skipped"):  c[(s["metric"], "SKIP: " + (s.get("skip_reason") or ""))] += 1
for (m, e), n in c.most_common():
    print(f"{n:3d}  {m:26s} {e}")
PY
```

## 2. Root cause: `403 Public access is disabled`

The Azure OpenAI resource has **public network access disabled** — it only
accepts requests over its approved Private Link / VNet path. Azure rejects the
request at the **network layer, before auth**, so:

- It is *not* a bad key, a bad deployment name, or a wrong api-version (the
  deployment can be healthy and the four `AZURE_OPENAI_*` values correct).
- **api-key vs AAD makes no difference** — a network policy is decided before the
  credential is read. The hr-agent's `AZURE_TENANT_ID` / `AZURE_CLIENT_ID` /
  `AZURE_CLIENT_SECRET` are for its **Cosmos DB / Blob** access (those accounts
  have local-auth disabled), **not** the LLM, which uses an api-key. Those vars
  are not useful to the eval and would not fix this.

## 3. The judge is built to match the hr-agent

The default judge backend, `langchain_azure`, builds the **same
`langchain_openai.AzureChatOpenAI` client the hr-agent uses** — api-key auth, the
same four `AZURE_OPENAI_*` env vars, no `max_tokens`. So if the hr-agent can reach
the model from the dev pod, the judge can too **given the same environment**.

The hr-agent runs as a **Chainlit server on the dev pod** (a normal process on the
same machine — its A2A Function app is unused). Same machine ⇒ same egress IP, so
a 403 from the eval while the hr-agent works means the eval's process is missing
something the server has: a **proxy**, or a **different endpoint value**.

## 4. Diagnose: diff the eval's environment against the hr-agent's

Inspect the environment the running Chainlit server actually uses (`/proc`, Linux):

```bash
pid=$(pgrep -af 'chainlit' | awk 'NR==1{print $1}')
echo "chainlit pid=$pid"
tr '\0' '\n' < /proc/$pid/environ | grep -iE 'AZURE_OPENAI|PROXY|NO_PROXY'   # the server
echo "--- eval shell ---"
env | grep -iE 'AZURE_OPENAI|PROXY|NO_PROXY'                                  # yours
```

Confirm reachability from the eval shell using the server's exact env:

```bash
eval "$(tr '\0' '\n' < /proc/$pid/environ | grep -E '^(AZURE_OPENAI_|HTTPS_PROXY|HTTP_PROXY|NO_PROXY)' | sed 's/^/export /')"
curl -sS -o /dev/null -w '%{http_code}\n' ${HTTPS_PROXY:+--proxy $HTTPS_PROXY} \
  -H "api-key: $AZURE_OPENAI_API_KEY" -H "content-type: application/json" \
  "$AZURE_OPENAI_ENDPOINT/openai/deployments/$AZURE_OPENAI_DEPLOYMENT_NAME/chat/completions?api-version=$AZURE_OPENAI_API_VERSION" \
  -d '{"messages":[{"role":"user","content":"hi"}],"max_completion_tokens":5}'
```

**Anything other than 403** (200/400/401) ⇒ the path is reachable; the eval just
needs the same vars. **Still 403 with the server's exact env** ⇒ verify the
Chainlit agent gets a live answer *right now*; if it doesn't either, the resource
was locked down after it last worked → use a reachable model (§6).

## 5. Fix A — environment parity (preferred: same model, no deployment change)

Put whatever the server has and the eval lacks into the eval's gitignored `.env`
(the CLI loads it with `override=True`, so `.env` is authoritative):

- **Proxy is the approved egress:**
  ```
  HTTPS_PROXY=<value>
  HTTP_PROXY=<value>
  NO_PROXY=azpriv-cloud.ubs.net,localhost,127.0.0.1   # keep the backend SSE call direct
  ```
- **Endpoint differs** (e.g. a gateway, not the raw `*.openai.azure.com` host):
  copy the server's `AZURE_OPENAI_ENDPOINT` (and the matching key / deployment /
  api-version) into `.env`.

Then:

```bash
agent-evals run --target devpod --suite hr --metrics primary --judge langchain_azure --sink jsonl
```

## 6. Fix B — point the judge at a reachable model (no deployment change)

The judge model is independent of the agent's model; it only scores text.

- **Internal gateway / another reachable Azure deployment** (Azure-protocol):
  keep `--judge langchain_azure`, set `AZURE_OPENAI_ENDPOINT` to it.
- **Public OpenAI or any OpenAI-compatible endpoint:**
  ```bash
  pip install -e ".[openai]"
  export OPENAI_API_KEY=...  OPENAI_BASE_URL=https://.../v1  OPENAI_JUDGE_MODEL=gpt-4o
  agent-evals run --target devpod --suite hr --metrics primary --judge openai --sink jsonl
  ```
- **Local model on the pod** (always reachable; quality = the local model):
  ```bash
  ollama serve & ollama pull llama3.1
  export OPENAI_BASE_URL=http://localhost:11434/v1  OPENAI_API_KEY=ollama  OPENAI_JUDGE_MODEL=llama3.1
  agent-evals run --target devpod --suite hr --metrics primary --judge openai --sink jsonl
  ```

## 7. Fix C — unblock with no LLM (degraded)

Emits real `Score`s for all 9 judge metrics so the pipeline aggregates end-to-end
(crude keyword/non-empty heuristics — not real quality):

```bash
agent-evals run --target devpod --suite hr --metrics primary --judge heuristic --sink jsonl
```
