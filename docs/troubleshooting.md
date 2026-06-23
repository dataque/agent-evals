# Troubleshooting: the judge can't reach Azure

`--judge azure_openai` failing on every call is almost always the judge unable to
reach the Azure endpoint, in two layers you hit in order: a **TLS/CA** failure
first, then — once that's fixed — a **network-policy `403`**. Both are
environmental, not key/deployment bugs.

## TLS / CA: `Connection error` / `CERTIFICATE_VERIFY_FAILED`

When `scores.jsonl` shows `Connection error` / `SSL: CERTIFICATE_VERIFY_FAILED — unable to get local issuer
certificate`, the certificate presented to the judge is signed by a CA that is in
**neither** certifi **nor** the pod's OS trust store. That's the fingerprint of a
**corporate TLS-intercepting proxy** (it substitutes its own cert, signed by an
internal CA) — or a private-CA endpoint. The backend already trusts that CA
(that's how it reaches the model), so it exists on the pod; the fix is to give
Python the same CA.

## Pin it down

```bash
H=<your AZURE_OPENAI_ENDPOINT host, e.g. my-resource.openai.azure.com>

# 1. Is certifi fine for normal public sites? (isolates the problem to the Azure host)
python3 -c "import httpx; print(httpx.get('https://example.com').status_code)"
#   200 -> certifi works; the Azure host specifically presents an untrusted cert

# 2. Who signed the cert you're handed? (a corporate issuer => TLS interception)
openssl s_client -connect $H:443 -servername $H </dev/null 2>/dev/null | openssl x509 -noout -issuer

# 3. What CA bundle does curl / the backend already trust?
env | grep -iE 'CA_BUNDLE|SSL_CERT|CA_CERT|REQUESTS_CA|NODE_EXTRA'   # already-exported corp bundle?
curl -sS -o /dev/null -w '%{http_code}\n' https://$H/                # curl trust it without -k?
curl -v https://$H/ 2>&1 | grep -iE 'CAfile|CApath'                  # the bundle curl uses
```

## Confirm the exact cause from Python

Run in the pod, with `.env` loaded. It makes one call on each trust path and
prints the full exception `__cause__` chain (which `scores.jsonl` flattens away):

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

Interpreting it:
- **Both** lines fail with `CERTIFICATE_VERIFY_FAILED` → neither store has the
  signing CA; get it (below).
- The OS-store line returns an HTTP error instead (e.g. `403 Public access is
  disabled`) → that path *reaches* Azure; it's a network-policy/egress issue, not
  a CA one.

## Fix — give Python the CA

Point Python at the corp bundle (the OpenAI SDK / httpx honor `SSL_CERT_FILE`, so
**no code change** is needed):

```bash
export SSL_CERT_FILE=<the CAfile curl uses, or the corp bundle env var from step 3>
# common locations: /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem
#                   /etc/ssl/certs/ca-certificates.crt
```

If no system bundle has it, extract the CAs the backend's JVM already trusts:

```bash
keytool -list -rfc -keystore "$JAVA_HOME/lib/security/cacerts" -storepass changeit > /tmp/corp-cas.pem
export SSL_CERT_FILE=/tmp/corp-cas.pem
```

Re-run the Python check above; once TLS passes you'll either succeed or hit the
network policy below.

`SSL_CERT_FILE` is the one that matters: the judge goes through the OpenAI SDK →
httpx, which honors `SSL_CERT_FILE` / `SSL_CERT_DIR` but **not**
`REQUESTS_CA_BUNDLE` (that's `requests`-only — harmless to set to the same path,
but it won't fix the judge). And don't run a target with `tls.use_truststore: true`
while relying on `SSL_CERT_FILE`: truststore injection patches SSL to the OS store
and overrides it — use `--target local`.

## Then: `403 Public access is disabled. Please configure private endpoint`

Once TLS passes you may get a `403` from Azure. This is a **network policy**, not
your key: the resource has **public network access disabled** and only accepts
traffic over its **Private Endpoint (Private Link)**. The backend reaches the
model because it hits the **private** endpoint (private DNS → a `10.x` IP); the
judge is going out via the corp proxy to the **public** endpoint (which is why it
needed the corp CA above), and public is blocked. The working path exists from
this pod — the fix is to put the judge on it.

Diagnose, then mirror the backend:

```bash
H=<azure-host>   # your AZURE_OPENAI_ENDPOINT host

# (a) Does the name resolve to a PRIVATE ip from this pod?
getent hosts $H                       # 10.x / 100.x => Private Link in DNS; public => not

# (b) What proxy is the judge using right now?
env | grep -iE 'https?_proxy|no_proxy'

# (c) How does the BACKEND reach it? (this is the path to copy)
tr '\0' '\n' < /proc/$(pgrep -af 'java|spring' | awk 'NR==1{print $1}')/environ \
  | grep -iE 'PROXY|NO_PROXY|AZURE_OPENAI|OPENAI_'
```

Fix, by what the diagnosis shows:

- **FQDN resolves to a private `10.x` and you're proxying it** → bypass the proxy
  for Azure so the judge connects straight to the private endpoint:
  ```bash
  export NO_PROXY="${NO_PROXY},.openai.azure.com,$H"
  # also unset HTTPS_PROXY/HTTP_PROXY if they were forcing the Azure host through the proxy
  ```
  Two gotchas: it only helps if `getent hosts $H` returns a (private) address — if
  it returns **nothing**, the proxy is your only egress and `NO_PROXY` just swaps
  the `403` for a DNS error (there's no direct route to reach). And `NO_PROXY` must
  be **exported in the launching shell**, not set in `.env`: the corp profile
  already exports it, and the harness's `load_dotenv()` won't override an
  already-set variable, so a `.env` edit is silently ignored.
- **Backend uses a specific proxy (not the MITM one)** → point the judge at it:
  `export HTTPS_PROXY=<the backend's proxy>` (and copy its `NO_PROXY`).
- **Backend points at a different endpoint host** (a `…privatelink.openai.azure.com`
  FQDN in its env) → set `AZURE_OPENAI_ENDPOINT` to match.

Rule of thumb: whatever `/proc/<backend-pid>/environ` shows for
`PROXY` / `NO_PROXY` / `AZURE_OPENAI_*`, replicate it — the backend proves that
route works from this pod. If the private endpoint genuinely isn't reachable from
your shell, point the judge at a **public-access** resource (another dev Azure
OpenAI, or a personal key via `--judge openai`), or use the offline judge below.

### Verify what the judge actually sees

If a fix "didn't take", confirm the judge's *effective* config instead of trusting
the `export`, and read a **fresh** result rather than a stale `scores.jsonl`:

```python
python3 - <<'PY'
import os, socket
from agent_evals.envfile import load_dotenv; load_dotenv()
from urllib.parse import urlparse

host = urlparse(os.environ.get("AZURE_OPENAI_ENDPOINT", "")).hostname or "?"
print("=== env this process actually has (after load_dotenv) ===")
for k in ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_DEPLOYMENT_NAME", "SSL_CERT_FILE",
          "HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy"):
    print(f"  {k:24s} = {os.environ.get(k)!r}")

print("\n=== direct DNS? (empty => the proxy is your only egress) ===")
try:
    print("  resolves to", socket.getaddrinfo(host, 443)[0][4])
except Exception as e:
    print("  NO direct route:", e)

print("\n=== does httpx proxy this host? (trust_env=True mirrors the judge) ===")
import httpx
url = f"https://{host}/openai/v1/models"
for trust in (True, False):
    try:
        r = httpx.Client(trust_env=trust, timeout=20).get(
            url, headers={"api-key": os.environ.get("AZURE_OPENAI_API_KEY", "")})
        print(f"  trust_env={trust}: HTTP {r.status_code}")
    except Exception as e:
        print(f"  trust_env={trust}: {type(e).__name__}: {str(e)[:90]}")

print("\n=== the real judge call (fresh) ===")
try:
    from openai import AzureOpenAI
    AzureOpenAI(api_key=os.environ["AZURE_OPENAI_API_KEY"],
                azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
                api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21")
    ).chat.completions.create(model=os.environ["AZURE_OPENAI_DEPLOYMENT_NAME"],
                              messages=[{"role": "user", "content": "ping"}], max_tokens=5)
    print("  OK")
except Exception as e:
    print("  FAIL:", type(e).__name__, "->", str(e)[:160])
PY
```

Reading it:
- `NO_PROXY` printed **without your host** → the `export` didn't reach this process (the `.env` trap above).
- `trust_env=True` → `403` → still proxying (your `NO_PROXY` isn't applied/matching).
- `trust_env=True` → `ConnectError`/DNS **and** direct DNS empty → `NO_PROXY` *is*
  working, but there's no direct route, so it can't help; the proxy → public →
  `403` is the only path, and you need the backend's route or a reachable model.

## Keep moving without the LLM

The offline judge needs no network — it emits real scores for all judge metrics
(degraded quality, but the full pipeline runs):

```bash
agent-evals run --target local --suite hr --metrics primary --judge heuristic --sink jsonl
```
