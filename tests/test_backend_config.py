"""E19, third provenance kind: what the backend's own config file declares.

The actuator meter cannot carry request options, and a deployment that does not
expose ``configprops`` cannot be asked for them at all. Reading the backend's
``application.yaml`` covers that gap without asking an operator to retype
anything, and unlike a declaration it is checkable: the file carries a digest.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
import yaml

from agent_evals.backend_config import (
    REDACTED,
    config_disagreements,
    deep_merge,
    describe_backend_config,
    extract_actuator,
    extract_llm,
    redact,
)

BASE = {
    "spring": {"ai": {
        "openai": {
            "api-key": "${OPENAI-API-KEY}",
            "base-url": "https://internal-host.example/",
            "timeout": "150s",
            "max-retries": 2,
            "chat": {"model": "gpt-5.2", "reasoning-effort": "none",
                     "user": "bff-service"},
        },
        "vectorstore": {"type": "none"},
    }},
    "management": {
        "metrics": {"enable": {"all": False, "gen_ai": True, "spring.ai": True}},
        "endpoints": {"web": {"exposure": {"include": "health,metrics"}}},
    },
}


@pytest.fixture
def config_file(tmp_path):
    p = tmp_path / "application.yaml"
    p.write_text(yaml.safe_dump(BASE))
    return p


# --- extraction -------------------------------------------------------------

def test_reasoning_effort_is_recovered_from_the_file():
    # the whole point: no meter can carry this, and configprops is often off
    assert extract_llm(BASE)["reasoning_effort"] == "none"
    assert extract_llm(BASE)["model"] == "gpt-5.2"


def test_request_options_beyond_identity_are_kept():
    llm = extract_llm(BASE)
    assert llm["timeout"] == "150s"
    assert llm["max_retries"] == "2"


def test_the_nested_options_form_is_read_too():
    # Spring AI accepts `chat.model` and `chat.options.model` for one setting
    nested = {"spring": {"ai": {"azure": {"openai": {"chat": {"options": {
        "deploymentName": "hr-chat-eu", "reasoningEffort": "high"}}}}}}}
    assert extract_llm(nested) == {"deployment": "hr-chat-eu", "reasoning_effort": "high"}


def test_a_file_with_no_spring_ai_block_yields_nothing():
    assert extract_llm({"server": {"port": 8080}}) == {}


def test_the_actuator_exposure_is_recorded():
    # it explains this run's own probe result in the same artifact
    assert extract_actuator(BASE) == {
        "exposure_include": "health,metrics",
        "metrics_enable": {"all": False, "gen_ai": True, "spring.ai": True}}


# --- secrets ----------------------------------------------------------------

def test_credentials_are_redacted_by_key_not_by_value():
    out = redact(BASE)
    assert out["spring"]["ai"]["openai"]["api-key"] == REDACTED
    assert "OPENAI-API-KEY" not in str(out)


def test_endpoints_are_redacted_too():
    # matches the judge block, which records a deployment name but never an
    # endpoint: a host name is infrastructure, not provenance
    assert redact(BASE)["spring"]["ai"]["openai"]["base-url"] == REDACTED
    assert "internal-host.example" not in str(redact(BASE))


def test_a_literal_secret_is_caught_even_when_it_looks_ordinary():
    assert redact({"client-secret": "hunter2"})["client-secret"] == REDACTED


def test_the_recorded_section_never_carries_a_credential(config_file):
    section = describe_backend_config([config_file])
    assert "OPENAI-API-KEY" not in str(section)
    assert "internal-host.example" not in str(section)
    assert section["llm"]["reasoning_effort"] == "none"


# --- reading ----------------------------------------------------------------

def test_a_file_is_recorded_with_a_digest(config_file):
    section = describe_backend_config([config_file])
    assert section["source"] == "configured_in_source"
    assert section["files"][0]["digest"].startswith("sha256:")
    assert section["files"][0]["bytes"] > 0


def test_a_directory_resolves_to_its_application_yaml(config_file):
    section = describe_backend_config([config_file.parent])
    assert section["files"][0]["path"].endswith("application.yaml")
    assert section["llm"]["model"] == "gpt-5.2"


def test_a_spring_boot_jar_is_read_from_boot_inf(tmp_path):
    jar = tmp_path / "bff-service.jar"
    with zipfile.ZipFile(jar, "w") as z:
        z.writestr("BOOT-INF/classes/application.yaml", yaml.safe_dump(BASE))
    section = describe_backend_config([jar])
    assert "!/BOOT-INF/classes/application.yaml" in section["files"][0]["path"]
    assert section["llm"]["reasoning_effort"] == "none"


def test_profiles_layer_left_to_right_as_spring_does(tmp_path):
    base = tmp_path / "application.yaml"
    base.write_text(yaml.safe_dump(BASE))
    profile = tmp_path / "application-local.yaml"
    profile.write_text(yaml.safe_dump(
        {"spring": {"ai": {"openai": {"chat": {"reasoning-effort": "high"}}}}}))
    section = describe_backend_config([base, profile])
    assert section["llm"]["reasoning_effort"] == "high"   # profile wins
    assert section["llm"]["model"] == "gpt-5.2"           # base survives
    assert len(section["files"]) == 2


def test_multi_document_files_are_merged(tmp_path):
    p = tmp_path / "application.yaml"
    p.write_text("spring:\n  ai:\n    openai:\n      chat:\n        model: a\n"
                 "---\nmanagement:\n  endpoints:\n    web:\n      exposure:\n"
                 "        include: health\n")
    section = describe_backend_config([p])
    assert section["llm"]["model"] == "a"
    assert section["actuator"]["exposure_include"] == "health"


def test_an_unreadable_path_is_recorded_not_raised(tmp_path):
    section = describe_backend_config([tmp_path / "nope.yaml"])
    assert "error" in section["files"][0]
    assert "llm" not in section


def test_malformed_yaml_is_recorded_not_raised(tmp_path):
    p = tmp_path / "application.yaml"
    p.write_text("spring:\n  ai:\n   - broken\n     indentation: [")
    assert "error" in describe_backend_config([p])["files"][0]


def test_no_configured_path_means_no_section():
    # a run that configured nothing must not grow an empty block
    assert describe_backend_config([]) == {}
    assert describe_backend_config("") == {}
    assert describe_backend_config(None) == {}


def test_deep_merge_layers_without_dropping_siblings():
    assert deep_merge({"a": {"x": 1, "y": 2}}, {"a": {"y": 3}}) == {"a": {"x": 1, "y": 3}}


# --- the cross-check the section exists to make possible ---------------------

def test_a_config_that_disagrees_with_the_running_backend_is_reported():
    section = {"llm": {"model": "gpt-5.2", "reasoning_effort": "none"}}
    lines = config_disagreements(section, {"model": "gpt-5.5"})
    assert len(lines) == 1
    assert "gpt-5.2" in lines[0] and "gpt-5.5" in lines[0]


def test_agreement_is_silent():
    section = {"llm": {"model": "gpt-5.5"}}
    assert config_disagreements(section, {"model": "gpt-5.5"}) == []


def test_a_field_the_meter_cannot_verify_is_never_called_a_disagreement():
    # reasoning_effort is absent from the observed block by construction, so it
    # must not be reported as a conflict with itself
    section = {"llm": {"reasoning_effort": "none"}}
    assert config_disagreements(section, {"model": "gpt-5.5"}) == []


def test_matching_any_accumulated_tag_value_is_agreement():
    section = {"llm": {"model": "gpt-4o"}}
    assert config_disagreements(section, {"model": ["gpt-4o", "gpt-5.5"]}) == []
