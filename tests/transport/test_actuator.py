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
        "api_version": "2024-10-21"}


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


def test_options_probe_records_both_failures_when_neither_endpoint_is_exposed():
    result = probe_backend_options(SSE_URL, http_transport=_routes({}))
    assert "reasoning_effort" not in result
    assert "configprops" in result["probe"]["error"] and "env" in result["probe"]["error"]


# --- the combined probe -----------------------------------------------------

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
