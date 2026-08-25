"""E19: read the backend's own LLM identity instead of taking the operator's word.

Spring AI publishes the OpenTelemetry GenAI meter ``gen_ai.client.operation``,
whose actuator representation names the request/response model and the provider.
It cannot name the request options (a Micrometer meter carries only the
convention's low-cardinality tags), so reasoning effort and API version come from
the actuator's configuration endpoints instead.
"""

from __future__ import annotations

import httpx
import pytest

from agent_evals.transport.actuator import (
    actuator_url,
    parse_configprops,
    parse_env,
    parse_env_context,
    parse_gen_ai_metric,
    probe_backend,
    probe_backend_model,
    probe_backend_options,
)

SSE_URL = "https://host.example/gw/api/v1/bff/ai/agent/sse"

METRIC_PAYLOAD = {
    "name": "gen_ai.client.operation",
    "measurements": [{"statistic": "COUNT", "value": 128.0},
                     {"statistic": "TOTAL_TIME", "value": 942.3}],
    "availableTags": [{"tag": "gen_ai.request.model", "values": ["gpt-5.5"]},
                      {"tag": "gen_ai.response.model", "values": ["gpt-5.5-2026-04-01"]},
                      {"tag": "gen_ai.system", "values": ["azure_openai"]},
                      {"tag": "gen_ai.operation.name", "values": ["chat"]}],
}

CONFIGPROPS_PAYLOAD = {
    "contexts": {"application": {"beans": {
        "spring.ai.azure.openai.chat": {
            "prefix": "spring.ai.azure.openai.chat",
            "properties": {"options": {"deploymentName": "hr-chat-eu",
                                       "reasoningEffort": "medium",
                                       "temperature": 0.7}}},
        "spring.ai.azure.openai": {
            "prefix": "spring.ai.azure.openai",
            "properties": {"apiKey": "******", "serviceVersion": "2024-10-21"}},
        "unrelated": {"prefix": "app.pricing", "properties": {"model": "NOT-AN-LLM"}},
    }}},
}


def _routes(mapping: dict[str, tuple[int, object]]):
    """A mock transport serving one payload per actuator path."""
    def handler(request: httpx.Request) -> httpx.Response:
        status, body = mapping.get(request.url.path, (404, {"error": "Not Found"}))
        return httpx.Response(status, json=body)
    return httpx.MockTransport(handler)


# --- URL derivation ---------------------------------------------------------

def test_actuator_url_keeps_the_gateway_base_path():
    # mirrors _graphql_url: strip the API path, keep whatever precedes it
    assert actuator_url(SSE_URL) == (
        "https://host.example/gw/actuator/metrics/gen_ai.client.operation")


def test_actuator_url_falls_back_to_the_origin():
    assert actuator_url("http://localhost:8080/sse") == (
        "http://localhost:8080/actuator/metrics/gen_ai.client.operation")


# --- the meter --------------------------------------------------------------

def test_metric_tags_become_model_provider_and_call_count():
    assert parse_gen_ai_metric(METRIC_PAYLOAD) == {
        "model": "gpt-5.5", "response_model": "gpt-5.5-2026-04-01",
        "provider": "azure_openai", "calls": 128}


def test_accumulated_tag_values_are_all_kept():
    # `values` is every model seen since boot. Several is real information (a
    # failover, or per-agent models) and collapsing it would hide the failover.
    payload = {"measurements": [{"statistic": "COUNT", "value": 9.0}],
               "availableTags": [{"tag": "gen_ai.request.model",
                                  "values": ["gpt-5.5", "gpt-4o", "gpt-5.5"]}]}
    assert parse_gen_ai_metric(payload)["model"] == ["gpt-4o", "gpt-5.5"]


def test_a_payload_with_no_model_tag_reads_as_nothing_learned():
    # "endpoint answered but told me nothing" must be indistinguishable from
    # "no endpoint" to the caller.
    assert parse_gen_ai_metric(
        {"availableTags": [{"tag": "gen_ai.operation.name", "values": ["chat"]}]}) == {}
    assert parse_gen_ai_metric({}) == {}


def test_probe_reports_model_and_call_count():
    transport = _routes({"/gw/actuator/metrics/gen_ai.client.operation": (200, METRIC_PAYLOAD)})
    result = probe_backend_model(SSE_URL, http_transport=transport)
    assert result["model"] == "gpt-5.5"
    assert result["provider"] == "azure_openai"
    assert result["probe"]["calls"] == 128


def test_a_lazily_registered_meter_404s_without_failing_the_run():
    # Before the backend's first LLM call the meter does not exist. That is a
    # legitimate answer, not an error the run should die on.
    result = probe_backend_model(SSE_URL, http_transport=_routes({}))
    assert "model" not in result
    assert "404" in result["probe"]["error"]


def test_the_404_message_distinguishes_a_lazy_meter_from_an_unexposed_endpoint():
    # only the meter is lazy. Telling a reader that a 404 on configprops might
    # mean "no LLM call yet" sends them chasing a warm-up problem that cannot
    # exist there.
    meter = probe_backend_model(SSE_URL, http_transport=_routes({}))["probe"]["error"]
    config = probe_backend_options(SSE_URL, http_transport=_routes({}))["probe"]["error"]
    assert "no LLM call yet" in meter
    assert "no LLM call yet" not in config
    assert "exposure.include" in config


def test_a_reachable_but_redacted_config_endpoint_says_so():
    # Spring Boot 3 defaults show-values to NEVER, redacting EVERY value
    redacted = {"contexts": {"application": {"beans": {"b": {
        "prefix": "spring.ai.azure.openai.chat",
        "properties": {"options": {"reasoningEffort": "******"}}}}}}}
    transport = _routes({"/gw/actuator/configprops": (200, redacted)})
    error = probe_backend_options(SSE_URL, http_transport=transport)["probe"]["error"]
    assert "show-values" in error


def test_a_probe_that_learns_nothing_still_records_that_it_asked():
    transport = _routes({"/gw/actuator/metrics/gen_ai.client.operation": (200, {})})
    probe = probe_backend_model(SSE_URL, http_transport=transport)["probe"]
    assert probe["url"].endswith("/gen_ai.client.operation")
    assert "no model tag" in probe["error"]


@pytest.mark.parametrize("status", [401, 403, 500])
def test_a_locked_down_or_broken_endpoint_is_non_fatal(status):
    transport = _routes({"/gw/actuator/metrics/gen_ai.client.operation": (status, {})})
    assert probe_backend_model(SSE_URL, http_transport=transport)["probe"]["error"] == (
        f"HTTP {status}")


def test_html_from_a_proxy_is_non_fatal():
    def handler(request):
        return httpx.Response(200, text="<html>gateway timeout</html>")
    result = probe_backend_model(SSE_URL, http_transport=httpx.MockTransport(handler))
    assert "model" not in result
    assert result["probe"]["error"]


def test_the_probe_authenticates_with_the_run_s_own_token():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json=METRIC_PAYLOAD)

    probe_backend_model(SSE_URL, token="tok-123", http_transport=httpx.MockTransport(handler))
    assert seen["auth"] == "Bearer tok-123"


# --- the config endpoints (reasoning effort is NOT on the meter) -------------

def test_configprops_yields_reasoning_effort_and_api_version():
    assert parse_configprops(CONFIGPROPS_PAYLOAD) == {
        "deployment": "hr-chat-eu", "reasoning_effort": "medium",
        "temperature": 0.7, "api_version": "2024-10-21"}


def test_the_settings_that_decide_whether_two_runs_are_comparable_are_read():
    # Identity is not enough for E19: a run at a different temperature, seed or
    # retry budget is not the same experiment, and nothing else in a bundle says
    # so. `parallel_tool_calls` moves trajectories, and so every trajectory scorer.
    payload = {"contexts": {"application": {"beans": {"b": {
        "prefix": "spring.ai.openai.chat",
        "properties": {"options": {"temperature": 0.7, "topP": 0.95, "seed": 42,
                                   "maxCompletionTokens": 4096, "verbosity": "low",
                                   "parallelToolCalls": False, "maxRetries": 2,
                                   "timeout": "150s", "serviceTier": "flex"}}}}}}}
    assert parse_configprops(payload) == {
        "temperature": 0.7, "top_p": 0.95, "seed": 42,
        "max_completion_tokens": 4096, "verbosity": "low",
        "parallel_tool_calls": False, "max_retries": 2,
        "timeout": "150s", "service_tier": "flex"}


def test_a_false_or_zero_setting_is_recorded_not_dropped_as_empty():
    # `parallel-tool-calls: false` and `temperature: 0` are deliberate settings.
    # Testing truthiness rather than emptiness would silently drop both, and the
    # bundle would then read as if the backend had never configured them.
    payload = {"contexts": {"application": {"beans": {"b": {
        "prefix": "spring.ai.openai.chat",
        "properties": {"options": {"parallelToolCalls": False, "temperature": 0}}}}}}}
    assert parse_configprops(payload) == {"parallel_tool_calls": False, "temperature": 0}


def test_a_sibling_embedding_bean_is_never_read_as_the_chat_model():
    # A service that also configures embeddings binds a second `model`. Recording
    # text-embedding-3-large as the model that answered the user would be wrong,
    # and with the wider field set it would drag that bean's options along too.
    payload = {"contexts": {"application": {"beans": {
        "embedding": {"prefix": "spring.ai.openai.embedding",
                      "properties": {"options": {"model": "text-embedding-3-large",
                                                 "dimensions": 3072}}},
        "chat": {"prefix": "spring.ai.openai.chat",
                 "properties": {"options": {"model": "gpt-5.2"}}}}}}}
    assert parse_configprops(payload) == {"model": "gpt-5.2"}


def test_a_longer_option_name_is_not_read_as_a_shorter_one():
    # env matches on a suffix, because SPRING_AI_..._TOP_LOGPROBS has no
    # separators left to split on. Shortest-first would read `top-logprobs` as
    # `logprobs` and `deployment-name` as `deployment`.
    payload = {"propertySources": [{"name": "applicationConfig", "properties": {
        "spring.ai.openai.chat.options.top-logprobs": {"value": 5},
        "spring.ai.openai.chat.options.deployment-name": {"value": "hr-chat-eu"}}}]}
    assert parse_env(payload) == {"top_logprobs": 5, "deployment": "hr-chat-eu"}


def test_only_spring_ai_beans_are_swept():
    # a blanket walk would read `model` off the unrelated pricing bean
    assert "NOT-AN-LLM" not in str(parse_configprops(CONFIGPROPS_PAYLOAD))


def test_redacted_values_are_dropped_not_recorded_as_stars():
    payload = {"contexts": {"application": {"beans": {"b": {
        "prefix": "spring.ai.azure.openai.chat",
        "properties": {"options": {"reasoningEffort": "******"}}}}}}}
    assert parse_configprops(payload) == {}


def test_env_is_the_fallback_and_honours_spring_precedence():
    payload = {"propertySources": [
        {"name": "systemEnvironment", "properties": {
            "SPRING_AI_AZURE_OPENAI_CHAT_OPTIONS_REASONING_EFFORT": {"value": "high"}}},
        {"name": "applicationConfig", "properties": {
            "spring.ai.azure.openai.chat.options.reasoning-effort": {"value": "medium"},
            "spring.ai.azure.openai.chat.options.deployment-name": {"value": "hr-chat-eu"},
            "app.pricing.model": {"value": "NOT-AN-LLM"}}}]}
    # property sources arrive in precedence order, so the env override wins
    assert parse_env(payload) == {"reasoning_effort": "high", "deployment": "hr-chat-eu"}


def test_options_probe_falls_back_from_configprops_to_env():
    env_payload = {"propertySources": [{"name": "applicationConfig", "properties": {
        "spring.ai.azure.openai.chat.options.reasoning-effort": {"value": "low"}}}]}
    transport = _routes({"/gw/actuator/env": (200, env_payload)})  # configprops 404s
    result = probe_backend_options(SSE_URL, http_transport=transport)
    assert result["reasoning_effort"] == "low"
    assert result["probe"]["url"].endswith("/actuator/env")


# --- what only `env` can answer ---------------------------------------------

def test_env_reports_the_active_profile():
    # activeProfiles decides which application-<profile>.yaml the pod merged, and
    # so which prompts and model settings it is running. A run compared against
    # one from another profile is not a comparison at all, and nothing else in
    # the bundle records which profile answered.
    assert parse_env_context({"activeProfiles": ["uat"]})["profiles"] == ["uat"]


def test_env_names_the_property_source_that_won_each_field():
    # configprops reports the value AFTER binding, so it cannot say that a
    # ConfigMap displaced the checked-in application.yaml. For an eval that must
    # be environment-independent, that displacement is the finding.
    payload = {"activeProfiles": ["dev"], "propertySources": [
        {"name": "systemEnvironment", "properties": {
            "SPRING_AI_OPENAI_CHAT_OPTIONS_REASONING_EFFORT": {"value": "high"}}},
        {"name": "applicationConfig: [classpath:/application.yaml]", "properties": {
            "spring.ai.openai.chat.options.reasoning-effort": {"value": "none"},
            "spring.ai.openai.chat.options.temperature": {"value": 0.7}}}]}
    context = parse_env_context(payload)
    assert context["property_sources"]["reasoning_effort"] == "systemEnvironment"
    assert context["property_sources"]["temperature"].startswith("applicationConfig")


def test_a_configprops_payload_carries_no_env_context():
    assert parse_env_context(CONFIGPROPS_PAYLOAD) == {}


# --- both endpoints, not the first that answers ------------------------------

def test_both_config_endpoints_are_read_and_configprops_wins_a_shared_field():
    # They are not interchangeable: configprops reports the bound value the model
    # actually uses, env is the only source of profiles and provenance. Stopping
    # at the first that answers throws the other half away.
    env_payload = {"activeProfiles": ["uat"], "propertySources": [
        {"name": "applicationConfig", "properties": {
            "spring.ai.azure.openai.chat.options.reasoning-effort": {"value": "STALE"}}}]}
    transport = _routes({"/gw/actuator/configprops": (200, CONFIGPROPS_PAYLOAD),
                         "/gw/actuator/env": (200, env_payload)})
    result = probe_backend_options(SSE_URL, http_transport=transport)
    assert result["reasoning_effort"] == "medium"      # configprops, not env
    assert result["profiles"] == ["uat"]               # env only
    assert result["options"]["temperature"] == 0.7     # observed-only detail
    assert "error" not in result["probe"]


def test_one_exposed_endpoint_is_a_result_not_a_half_failure():
    # Exposing configprops alone is the right call on a shared deployment, since
    # `env` with show-values ALWAYS hands every resolved secret to any caller.
    # That choice must not stain the probe block with a top-level error.
    transport = _routes({"/gw/actuator/configprops": (200, CONFIGPROPS_PAYLOAD)})
    probe = probe_backend_options(SSE_URL, http_transport=transport)["probe"]
    assert "error" not in probe
    assert probe["url"].endswith("/configprops")
    assert "404" in probe["endpoints"]["env"]["error"]   # recorded, not hidden


def test_options_probe_records_both_failures_when_neither_endpoint_is_exposed():
    result = probe_backend_options(SSE_URL, http_transport=_routes({}))
    assert "reasoning_effort" not in result
    assert "configprops" in result["probe"]["error"] and "env" in result["probe"]["error"]


# --- the combined probe -----------------------------------------------------

def test_the_combined_probe_carries_the_observed_only_detail_across():
    # options/profiles/property_sources answer to no meter and no declaration,
    # so they are carried whole rather than merged field by field.
    env_payload = {"activeProfiles": ["uat"], "propertySources": [
        {"name": "applicationConfig", "properties": {
            "spring.ai.azure.openai.chat.options.max-retries": {"value": 2}}}]}
    transport = _routes({
        "/gw/actuator/metrics/gen_ai.client.operation": (200, METRIC_PAYLOAD),
        "/gw/actuator/configprops": (200, CONFIGPROPS_PAYLOAD),
        "/gw/actuator/env": (200, env_payload)})
    result = probe_backend(SSE_URL, http_transport=transport)
    assert result["model"] == "gpt-5.5"                    # the meter still wins
    assert result["options"]["temperature"] == 0.7         # configprops
    assert result["options"]["max_retries"] == 2           # env filled the gap
    assert result["profiles"] == ["uat"]


def test_combined_probe_merges_the_meter_with_the_config_endpoints():
    transport = _routes({
        "/gw/actuator/metrics/gen_ai.client.operation": (200, METRIC_PAYLOAD),
        "/gw/actuator/configprops": (200, CONFIGPROPS_PAYLOAD)})
    result = probe_backend(SSE_URL, http_transport=transport)
    assert result["model"] == "gpt-5.5"                 # from the meter
    assert result["reasoning_effort"] == "medium"       # from configprops
    assert result["api_version"] == "2024-10-21"
    assert result["probe"]["metrics"]["calls"] == 128
    assert result["probe"]["config"]["url"].endswith("/configprops")


def test_the_meter_outranks_config_on_model_identity():
    # configprops describes what the pod was CONFIGURED with; the meter is a
    # record of calls that actually happened, so it wins on identity.
    stale = {"contexts": {"application": {"beans": {"b": {
        "prefix": "spring.ai.azure.openai.chat",
        "properties": {"options": {"model": "gpt-4o", "reasoningEffort": "medium"}}}}}}}
    transport = _routes({
        "/gw/actuator/metrics/gen_ai.client.operation": (200, METRIC_PAYLOAD),
        "/gw/actuator/configprops": (200, stale)})
    result = probe_backend(SSE_URL, http_transport=transport)
    assert result["model"] == "gpt-5.5"
    assert result["reasoning_effort"] == "medium"


def test_config_fills_the_model_only_when_the_meter_cannot():
    stale = {"contexts": {"application": {"beans": {"b": {
        "prefix": "spring.ai.azure.openai.chat",
        "properties": {"options": {"model": "gpt-4o"}}}}}}}
    transport = _routes({"/gw/actuator/configprops": (200, stale)})  # meter 404s
    result = probe_backend(SSE_URL, http_transport=transport)
    assert result["model"] == "gpt-4o"
    assert "404" in result["probe"]["metrics"]["error"]


def test_a_backend_with_no_actuator_at_all_is_survivable():
    result = probe_backend(SSE_URL, http_transport=_routes({}))
    assert [k for k in result if k != "probe"] == []
    assert result["probe"]["metrics"]["error"] and result["probe"]["config"]["error"]
