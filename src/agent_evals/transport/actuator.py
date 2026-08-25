"""Ask the backend which model it is running, via its Spring Boot actuator.

E19's durable half. The AG-UI stream never names the model and ``usage.by_model``
is null on every turn, so a run bundle could only ever carry the operator's word
for what produced it. Spring AI's Micrometer instrumentation does know: it
publishes the OpenTelemetry GenAI convention meter ``gen_ai.client.operation``,
whose tags carry the request/response model and the provider.

``GET /actuator/metrics/gen_ai.client.operation`` returns::

    {"name": "gen_ai.client.operation",
     "measurements": [{"statistic": "COUNT", "value": 128.0}, ...],
     "availableTags": [{"tag": "gen_ai.request.model", "values": ["gpt-4o"]},
                       {"tag": "gen_ai.system", "values": ["azure_openai"]}, ...]}

Two properties of that endpoint shape everything here:

* **The meter is registered lazily.** Before the backend's first LLM call the
  endpoint 404s, so a probe against a freshly booted pod legitimately finds
  nothing. Probe again after the run, when the eval's own traffic has guaranteed
  the meter exists.
* **Tags accumulate.** ``values`` is every model seen since boot, not the one in
  use now. Several values is real information (a failover, or per-agent models),
  so all of them are recorded rather than collapsed to the first.

**Reasoning effort is not on that meter, and never will be.** A Micrometer meter
carries only the low-cardinality keys of the GenAI convention (operation, system,
request model, response model); per-request options such as reasoning effort,
temperature and API version are high-cardinality and are dropped before they
reach a metric. Those settings do surface in the actuator's *configuration*
endpoints, so ``probe_backend_options`` asks those instead: ``configprops``
first, which has already bound them to the Spring AI chat-options bean, falling
back to ``env`` when configprops is not exposed.

Every failure here is non-fatal: a run must never abort because a metrics
endpoint is absent, unauthenticated, or behind a proxy that returns HTML.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx

METRIC = "gen_ai.client.operation"

_TAG_FIELDS = {
    "gen_ai.request.model": "model",
    "gen_ai.response.model": "response_model",
    "gen_ai.system": "provider",
}

# Chat-option properties worth recording, keyed by their NORMALISED name, so
# that `reasoningEffort` (configprops binds to bean properties), the
# `reasoning-effort` of a YAML property source and the `REASONING_EFFORT` of an
# environment override all land on the same field. Order matters: the longer
# `deploymentname` must be tried before `deployment`.
_OPTION_FIELDS = {
    "reasoningeffort": "reasoning_effort",
    "apiversion": "api_version",
    "serviceversion": "api_version",
    "deploymentname": "deployment",
    "deployment": "deployment",
    "model": "model",
}

# Actuator redacts values it considers secret rather than omitting the key.
_SANITISED = "******"


def _actuator_base(base_url: str) -> str:
    """The actuator root for a backend addressed by its chat SSE URL."""
    url = (base_url or "").rstrip("/")
    if "/api/" in url:  # strip the API path, keep any gateway base path
        return url.split("/api/", 1)[0] + "/actuator"
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}/actuator"


def actuator_url(base_url: str, metric: str = METRIC) -> str:
    """Derive the metric endpoint from the chat SSE URL.

    Mirrors ``_graphql_url``: strip the API path so any gateway base path is
    kept, else fall back to the origin.
    """
    return f"{_actuator_base(base_url)}/metrics/{metric}"


def _normalise(key: object) -> str:
    return "".join(ch for ch in str(key).lower() if ch.isalnum())


def parse_gen_ai_metric(payload: dict) -> dict:
    """Extract model/provider from an actuator metric response.

    Returns ``{}`` for a payload that carries no usable tag, so a caller can
    treat "endpoint answered but told me nothing" the same as "no endpoint".
    """
    out: dict = {}
    for entry in (payload or {}).get("availableTags") or []:
        field = _TAG_FIELDS.get((entry or {}).get("tag"))
        if not field:
            continue
        values = [str(v).strip() for v in (entry.get("values") or []) if str(v).strip()]
        if not values:
            continue
        # Tags accumulate since boot, so more than one value is a real finding
        # (a failover, or per-agent models) and must not be collapsed away.
        out[field] = values[0] if len(values) == 1 else sorted(set(values))
    if not out:
        return {}
    for measurement in (payload or {}).get("measurements") or []:
        if (measurement or {}).get("statistic") != "COUNT":
            continue
        try:
            out["calls"] = int(float(measurement.get("value")))
        except (TypeError, ValueError):
            pass
        break
    return out


def _collect_options(node: object, out: dict) -> None:
    """Depth-first sweep of a bound properties tree for chat-option leaves."""
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, (dict, list)):
                _collect_options(value, out)
                continue
            field = _OPTION_FIELDS.get(_normalise(key))
            if not field or field in out:
                continue
            text = str(value).strip() if value is not None else ""
            if text and text != _SANITISED:
                out[field] = text
    elif isinstance(node, list):
        for item in node:
            _collect_options(item, out)


def parse_configprops(payload: dict) -> dict:
    """Chat options from ``/actuator/configprops``.

    Only ``spring.ai.*`` beans are swept. A blanket walk of that document would
    happily read a ``model`` key off an unrelated bean and report it as the LLM.
    """
    out: dict = {}
    for context in ((payload or {}).get("contexts") or {}).values():
        for bean in ((context or {}).get("beans") or {}).values():
            if not _normalise((bean or {}).get("prefix") or "").startswith("springai"):
                continue
            _collect_options((bean or {}).get("properties") or {}, out)
    return out


def parse_env(payload: dict) -> dict:
    """Chat options from ``/actuator/env``, the fallback when configprops is off.

    Property sources arrive in Spring's own precedence order, so the first
    source carrying a key wins, exactly as the backend resolved it.
    """
    out: dict = {}
    for source in (payload or {}).get("propertySources") or []:
        for key, entry in ((source or {}).get("properties") or {}).items():
            norm = _normalise(key)
            if "springai" not in norm:
                continue
            for suffix, field in _OPTION_FIELDS.items():
                if not norm.endswith(suffix):
                    continue
                if field not in out:
                    text = str((entry or {}).get("value") or "").strip()
                    if text and text != _SANITISED:
                        out[field] = text
                break
    return out


def _parse_options(payload: dict) -> dict:
    return parse_configprops(payload) or parse_env(payload)


def _get_json(
    url: str,
    *,
    token: str | None,
    verify: bool | str,
    timeout_s: float,
    http_transport: "httpx.BaseTransport | None",
) -> tuple[dict | None, str | None]:
    """GET one JSON actuator endpoint as ``(payload, error)``. Never raises."""
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    kwargs: dict = {"timeout": httpx.Timeout(timeout_s), "verify": verify}
    if http_transport is not None:
        kwargs["transport"] = http_transport
    try:
        with httpx.Client(**kwargs) as client:
            resp = client.get(url, headers=headers)
        if resp.status_code == 404:
            return None, "HTTP 404 (endpoint not exposed, or no LLM call yet)"
        if resp.status_code >= 400:
            return None, f"HTTP {resp.status_code}"
        # A proxy that answers with an HTML error page fails here, not upstream.
        return resp.json(), None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def probe_backend_model(
    base_url: str,
    *,
    url: str | None = None,
    metric: str = METRIC,
    token: str | None = None,
    verify: bool | str = True,
    timeout_s: float = 5.0,
    http_transport: "httpx.BaseTransport | None" = None,
) -> dict:
    """Read the backend's model identity. Never raises.

    On success: the parsed fields plus ``probe`` (the URL and, when the endpoint
    reported it, the call count). On failure: only ``probe``, carrying the URL
    and a short ``error``, so a run bundle records that the harness asked and
    what it got back rather than staying silently empty.
    """
    target = url or actuator_url(base_url, metric)
    probe: dict = {"url": target}
    payload, error = _get_json(target, token=token, verify=verify, timeout_s=timeout_s,
                               http_transport=http_transport)
    if error:
        probe["error"] = error
        return {"probe": probe}
    parsed = parse_gen_ai_metric(payload or {})
    if not parsed:
        probe["error"] = "no model tag on " + target
        return {"probe": probe}
    calls = parsed.pop("calls", None)
    if calls is not None:
        probe["calls"] = calls
    return {**parsed, "probe": probe}


def probe_backend_options(
    base_url: str,
    *,
    url: str | None = None,
    token: str | None = None,
    verify: bool | str = True,
    timeout_s: float = 5.0,
    http_transport: "httpx.BaseTransport | None" = None,
) -> dict:
    """Read reasoning effort / API version from the backend's own config. Never raises.

    ``gen_ai.client.operation`` cannot answer this (see the module docstring), so
    the configuration endpoints are asked: ``configprops`` first, ``env`` as the
    fallback. Both are commonly locked down harder than ``metrics``, which is why
    every failure just becomes an ``error`` string on the probe block.
    """
    candidates = [url] if url else [f"{_actuator_base(base_url)}/configprops",
                                    f"{_actuator_base(base_url)}/env"]
    errors: list[str] = []
    for candidate in candidates:
        payload, error = _get_json(candidate, token=token, verify=verify, timeout_s=timeout_s,
                                   http_transport=http_transport)
        if error:
            errors.append(f"{candidate}: {error}")
            continue
        parsed = _parse_options(payload or {})
        if parsed:
            return {**parsed, "probe": {"url": candidate}}
        errors.append(f"{candidate}: no spring.ai chat options")
    return {"probe": {"url": candidates[0], "error": "; ".join(errors)}}


def probe_backend(
    base_url: str,
    *,
    token: str | None = None,
    verify: bool | str = True,
    timeout_s: float = 5.0,
    http_transport: "httpx.BaseTransport | None" = None,
    options: bool = True,
) -> dict:
    """Everything the backend will admit about its own LLM. Never raises.

    The meter is authoritative on identity, because it is a record of calls that
    actually happened; the configuration endpoints only fill in what a meter
    structurally cannot carry, and are never allowed to overwrite it. Both probe
    blocks are kept, so a bundle shows which endpoint answered and which did not.
    """
    observed = probe_backend_model(base_url, token=token, verify=verify, timeout_s=timeout_s,
                                   http_transport=http_transport)
    probes = {"metrics": observed.pop("probe", {})}
    if options:
        opts = probe_backend_options(base_url, token=token, verify=verify, timeout_s=timeout_s,
                                     http_transport=http_transport)
        probes["config"] = opts.pop("probe", {})
        for field, value in opts.items():
            observed.setdefault(field, value)
    observed["probe"] = probes
    return observed
