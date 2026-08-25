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
first, which has already bound them to the Spring AI chat-options bean, and
``env`` as well.

Both are asked because they answer different questions. configprops reports each
value *after* binding, which is what the model actually uses. ``env`` reports the
property sources in precedence order, and is therefore the only one that can say
which **active profile** the pod merged and **which source won** a given
setting; a value that came from a ConfigMap rather than the checked-in
``application.yaml`` is a finding an eval meant to be environment-independent
needs to see, and configprops structurally cannot show it.

What is worth reading goes well past model identity. The settings that decide
whether two runs are even comparable (``temperature``, ``top_p``, ``seed``), the
ones that bound the answer (``max_completion_tokens``), the ones that shape the
*measurement* rather than the answer (``timeout``, ``max_retries``: a retried
call inflates a latency percentile), and the one that moves agent trajectories
and so every trajectory-based scorer (``parallel_tool_calls``) are all here.
Only ``model``, ``deployment``, ``reasoning_effort`` and ``api_version`` are
promoted alongside the meter's fields, because those are the only ones an
operator can also declare and therefore be caught contradicting; the rest are
observed-only and land under ``options``.

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
# environment override all land on the same field.
#
# The output names match ``appconfig._MODEL_FIELDS`` deliberately: `options`
# here records what the RUNNING process holds, `backend_config` records what the
# build declared, and naming them alike lets the two be diffed field for field.
# The two disagreeing is the finding that pair of sections exists to surface.
_OPTION_FIELDS = {
    # identity and routing
    "model": "model",
    "deployment": "deployment",
    "deploymentname": "deployment",
    "microsoftdeploymentname": "deployment",
    "apiversion": "api_version",
    "serviceversion": "api_version",
    "microsoftfoundryserviceversion": "api_version",
    "baseurl": "base_url",
    "organizationid": "organization_id",
    # what shapes the ANSWER: two runs on different values are not comparable,
    # and nothing else in a bundle says they differed
    "temperature": "temperature",
    "topp": "top_p",
    "seed": "seed",
    "frequencypenalty": "frequency_penalty",
    "presencepenalty": "presence_penalty",
    "logprobs": "logprobs",
    "toplogprobs": "top_logprobs",
    "reasoningeffort": "reasoning_effort",
    "verbosity": "verbosity",
    "maxtokens": "max_tokens",
    "maxcompletiontokens": "max_completion_tokens",
    "responseformat": "response_format",
    # what shapes the MEASUREMENT rather than the answer. A retried call inflates
    # a latency percentile, so a bundle that does not say retries were possible
    # cannot explain its own tail.
    "timeout": "timeout",
    "maxretries": "max_retries",
    "servicetier": "service_tier",
    "store": "store",
    "promptcachekey": "prompt_cache_key",
    # what shapes the TRAJECTORY, and so every trajectory-based scorer
    "paralleltoolcalls": "parallel_tool_calls",
}

# Promoted to the top of the ``backend`` block: the fields an operator can also
# declare, and therefore the only ones a declared/observed mismatch can be
# computed for. Everything else is observed-only and lands under ``options``.
_CORE_FIELDS = ("model", "deployment", "reasoning_effort", "api_version")

# Blocks the config endpoints contribute whole, rather than field by field: no
# meter and no operator can supply them, so there is nothing to merge against.
OBSERVED_ONLY_DETAIL = ("options", "profiles", "property_sources")

# ``parse_env`` has to match on a SUFFIX, because an environment override
# arrives as SPRING_AI_..._REASONING_EFFORT with no separators left to split on.
# Longest first, so `top-logprobs` cannot be read as `logprobs`, nor
# `deployment-name` as `deployment`.
_OPTION_SUFFIXES = tuple(sorted(_OPTION_FIELDS.items(), key=lambda kv: -len(kv[0])))

# Sibling modalities that carry their own `model`, `temperature` and friends.
# The agent's LLM is the chat one: recording an embedding model as the model
# that answered the user would be simply wrong. Mirrors the same guard in
# ``appconfig``, which reads the identical settings off disk.
_NON_CHAT_MODALITIES = {"embedding", "embeddings", "image", "images", "audio",
                        "speech", "transcription", "moderation", "vectorstore"}

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


def _non_chat_modality(key: object) -> bool:
    """Whether a property key or bean prefix belongs to a non-chat modality.

    Splits on every separator Spring might use, so the dotted
    ``spring.ai.openai.embedding.options.model`` and the environment form
    ``SPRING_AI_OPENAI_EMBEDDING_OPTIONS_MODEL`` are both caught.
    """
    text = str(key).replace("_", ".").replace("-", ".")
    return any(_normalise(part) in _NON_CHAT_MODALITIES for part in text.split("."))


def _option_value(value: object) -> object | None:
    """A recordable option value, or ``None`` for one worth dropping.

    Numbers and booleans keep their own type, so ``temperature: 0.7`` and
    ``parallel_tool_calls: false`` read as themselves in the bundle rather than
    as strings. ``false`` and ``0`` are meaningful settings, so the emptiness
    test is on the rendered text, never on the value's truthiness.
    """
    if value is None or isinstance(value, (dict, list)):
        return None
    text = str(value).strip()
    if not text or text == _SANITISED:
        return None
    return value if isinstance(value, (bool, int, float)) else text


def _collect_options(node: object, out: dict) -> None:
    """Depth-first sweep of a bound properties tree for chat-option leaves."""
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, (dict, list)):
                # a chat bean can still nest an embedding sub-tree of its own
                if not _non_chat_modality(key):
                    _collect_options(value, out)
                continue
            field = _OPTION_FIELDS.get(_normalise(key))
            if not field or field in out:
                continue
            recorded = _option_value(value)
            if recorded is not None:
                out[field] = recorded
    elif isinstance(node, list):
        for item in node:
            _collect_options(item, out)


def parse_configprops(payload: dict) -> dict:
    """Chat options from ``/actuator/configprops``.

    Only ``spring.ai.*`` beans are swept. A blanket walk of that document would
    happily read a ``model`` key off an unrelated bean and report it as the LLM.
    Non-chat modalities are skipped for the same reason from the other side: a
    service that also configures embeddings binds a second ``model``.
    """
    out: dict = {}
    for context in ((payload or {}).get("contexts") or {}).values():
        for bean in ((context or {}).get("beans") or {}).values():
            prefix = (bean or {}).get("prefix") or ""
            if not _normalise(prefix).startswith("springai") or _non_chat_modality(prefix):
                continue
            _collect_options((bean or {}).get("properties") or {}, out)
    return out


def _scan_env(payload: dict) -> tuple[dict, dict]:
    """Chat options from ``/actuator/env``, with the source that supplied each.

    Property sources arrive in Spring's own precedence order, so the first
    source carrying a key wins, exactly as the backend resolved it.
    """
    out: dict = {}
    origin: dict = {}
    for source in (payload or {}).get("propertySources") or []:
        name = str((source or {}).get("name") or "").strip()
        for key, entry in ((source or {}).get("properties") or {}).items():
            norm = _normalise(key)
            if "springai" not in norm or _non_chat_modality(key):
                continue
            for suffix, field in _OPTION_SUFFIXES:
                if not norm.endswith(suffix):
                    continue
                if field not in out:
                    recorded = _option_value((entry or {}).get("value"))
                    if recorded is not None:
                        out[field] = recorded
                        if name:
                            origin[field] = name
                break
    return out, origin


def parse_env(payload: dict) -> dict:
    """Chat options from ``/actuator/env``, the fallback when configprops is off."""
    return _scan_env(payload)[0]


def parse_env_context(payload: dict) -> dict:
    """What only ``env`` can answer: active profiles, and where each value came from.

    ``activeProfiles`` decides which ``application-<profile>.yaml`` the pod
    merged, and so which prompts and model settings it is actually running.
    Nothing else in a bundle records it, and a run compared against one from a
    different profile is not a comparison at all.

    ``property_sources`` names the source that won each field. configprops
    structurally cannot answer this: it reports the value AFTER binding, so it
    cannot say whether a ConfigMap or an environment override displaced the
    checked-in ``application.yaml``. For an eval that is meant to be environment
    independent, that displacement is a finding rather than noise.
    """
    out: dict = {}
    profiles = [str(p).strip() for p in ((payload or {}).get("activeProfiles") or [])
                if str(p).strip()]
    if profiles:
        out["profiles"] = profiles
    origin = _scan_env(payload)[1]
    if origin:
        out["property_sources"] = origin
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
    not_found: str = "HTTP 404 (endpoint not exposed)",
) -> tuple[dict | None, str | None]:
    """GET one JSON actuator endpoint as ``(payload, error)``. Never raises.

    ``not_found`` spells out what a 404 means for THIS endpoint. Only the meter
    is registered lazily; a config endpoint that 404s is simply not exposed, and
    saying otherwise sends a reader looking for a warm-up problem that does not
    exist.
    """
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
            return None, not_found
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
                               http_transport=http_transport,
                               not_found="HTTP 404 (meter not registered: endpoint not exposed, "
                                         "or the backend has made no LLM call yet)")
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


def _endpoint_name(url: str) -> str:
    """The actuator endpoint a URL addresses, for keying the probe block."""
    return (url or "").rstrip("/").rsplit("/", 1)[-1] or "config"


def probe_backend_options(
    base_url: str,
    *,
    url: str | None = None,
    token: str | None = None,
    verify: bool | str = True,
    timeout_s: float = 5.0,
    http_transport: "httpx.BaseTransport | None" = None,
) -> dict:
    """Read the running backend's chat options from its own config. Never raises.

    ``gen_ai.client.operation`` cannot answer these (see the module docstring),
    so the configuration endpoints are asked.

    BOTH are asked, not merely the first that answers. They are not
    interchangeable: configprops reports values AFTER binding, which is what the
    model actually uses, while ``env`` is the only place the active profiles and
    the winning property source appear. configprops is asked first and wins any
    field they share, for that same reason.

    Both are commonly locked down harder than ``metrics``, so each endpoint's
    outcome is recorded separately under ``probe.endpoints`` and a top-level
    ``error`` is set only when NEITHER answered. One exposed endpoint is a
    complete result, not a half-failure.
    """
    candidates = [url] if url else [f"{_actuator_base(base_url)}/configprops",
                                    f"{_actuator_base(base_url)}/env"]
    fields: dict = {}
    context: dict = {}
    context_fields: dict = {}
    endpoints: dict = {}
    errors: list[str] = []
    answered: str | None = None
    for candidate in candidates:
        entry: dict = {"url": candidate}
        payload, error = _get_json(
            candidate, token=token, verify=verify, timeout_s=timeout_s,
            http_transport=http_transport,
            not_found="HTTP 404 (endpoint not exposed; add it to "
                      "management.endpoints.web.exposure.include)")
        if error:
            entry["error"] = error
        else:
            parsed = _parse_options(payload or {})
            extra = parse_env_context(payload or {})
            if not parsed and not extra:
                # Reached the endpoint but read nothing: on Spring Boot 3 this is
                # almost always show-values, which defaults to NEVER and redacts
                # EVERY value to `******`, not just the secrets.
                entry["error"] = ("no readable spring.ai chat options (if values are "
                                  "redacted, set management.endpoint.<name>.show-values "
                                  "to ALWAYS)")
            else:
                entry["fields"] = len(parsed)
                for key, value in parsed.items():
                    fields.setdefault(key, value)
                if extra and not context:
                    context, context_fields = dict(extra), parsed
                answered = answered or candidate
        if entry.get("error"):
            errors.append(f"{candidate}: {entry['error']}")
        endpoints[_endpoint_name(candidate)] = entry

    # Provenance describes the value that was RECORDED. Where configprops and env
    # disagree on a field (a bound Duration rendered differently from its raw
    # `150s`, say) configprops wins the value, so naming env's source for it
    # would attribute the recorded value to a source that did not supply it.
    # Drop those rather than assert something false.
    origin = {field: source for field, source in (context.get("property_sources") or {}).items()
              if field in fields and context_fields.get(field) == fields[field]}
    if origin:
        context["property_sources"] = origin
    else:
        context.pop("property_sources", None)

    probe: dict = {"url": answered or candidates[0]}
    if len(endpoints) > 1:
        probe["endpoints"] = endpoints
    if answered is None:
        probe["error"] = "; ".join(errors)
    out: dict = {k: v for k, v in fields.items() if k in _CORE_FIELDS}
    options = {k: v for k, v in fields.items() if k not in _CORE_FIELDS}
    if options:
        out["options"] = options
    out.update(context)
    out["probe"] = probe
    return out


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

    ``options``, ``profiles`` and ``property_sources`` are observed-only detail
    that no meter and no operator can supply, so they are carried across whole
    rather than merged field by field.
    """
    observed = probe_backend_model(base_url, token=token, verify=verify, timeout_s=timeout_s,
                                   http_transport=http_transport)
    probes = {"metrics": observed.pop("probe", {})}
    if options:
        opts = probe_backend_options(base_url, token=token, verify=verify, timeout_s=timeout_s,
                                     http_transport=http_transport)
        probes["config"] = opts.pop("probe", {})
        for field in OBSERVED_ONLY_DETAIL:
            value = opts.pop(field, None)
            if value:
                observed[field] = value
        for field, value in opts.items():
            observed.setdefault(field, value)
    observed["probe"] = probes
    return observed
