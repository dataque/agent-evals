"""E19: what the backend's application.yaml DECLARES, as its own params section.

The GenAI meter records what the running process did but structurally cannot
carry a request option; the actuator's config endpoints can but are often not
exposed. The backend's own YAML carries them plainly, and the eval runs beside
the backend, so it can just read the file.

Kept separate from the observed `backend` block on purpose: the two disagreeing
is the finding, not a conflict to resolve.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from agent_evals.appconfig import read_backend_config

BFF = textwrap.dedent("""
    spring:
      ai:
        chat:
          observations:
            log-prompt: false
        openai:
          api-key: ${OPENAI-API-KEY}
          timeout: 150s
          max-retries: 2
          chat:
            model: gpt-5.2
            reasoning-effort: none
            user: ${spring.application.name}
    management:
      endpoints:
        web:
          exposure:
            include: health,metrics
    """)

# a sibling service in the same monorepo that configures no LLM at all
OTHER_SERVICE = textwrap.dedent("""
    spring:
      application:
        name: skills-service
      datasource:
        model: not-an-llm
    """)


def _service(root: Path, name: str, body: str) -> Path:
    d = root / name / "src" / "main" / "resources"
    d.mkdir(parents=True)
    path = d / "application.yaml"
    path.write_text(body)
    return path


def _workspace(tmp_path: Path) -> Path:
    """A pod-shaped layout: the eval's cwd with sibling repos beside it.

    Nested inside tmp_path on purpose, because discovery also sweeps the PARENT
    of the working directory (that is how it finds the backend beside the eval),
    and an unnested root would reach other tests' fixtures.
    """
    cwd = tmp_path / "projects" / "agent-evals"
    cwd.mkdir(parents=True)
    return cwd


def test_model_settings_are_read_from_the_backend_yaml(tmp_path):
    cwd = _workspace(tmp_path)
    _service(cwd.parent, "backend", BFF)

    section = read_backend_config(search_from=cwd)
    assert section["model"] == "gpt-5.2"
    assert section["reasoning_effort"] == "none"
    assert section["timeout"] == "150s"
    assert section["max_retries"] == 2
    assert section["source"] == "application.yaml"
    assert section["files"] == ["application.yaml"]


def test_the_service_that_configures_no_llm_is_not_mistaken_for_the_backend(tmp_path):
    # a service monorepo has one application.yaml per service and only one of
    # them configures a model; `datasource.model` must not be read as the LLM
    cwd = _workspace(tmp_path)
    _service(cwd.parent, "aaa-first-alphabetically", OTHER_SERVICE)
    _service(cwd.parent, "zzz-backend", BFF)

    section = read_backend_config(search_from=cwd)
    assert section["model"] == "gpt-5.2"
    assert "zzz-backend" in section["path"]


def test_no_backend_source_nearby_records_that_it_looked(tmp_path):
    # distinguishable from "the harness never looked"
    section = read_backend_config(search_from=_workspace(tmp_path))
    assert "no application.yaml" in section["error"]
    assert section["searched"]


def test_an_explicit_path_is_taken_at_its_word(tmp_path):
    path = _service(tmp_path, "somewhere-else", BFF)
    section = read_backend_config(path=str(path), search_from=_workspace(tmp_path))
    assert section["model"] == "gpt-5.2"


# --- secrets -----------------------------------------------------------------

def test_a_real_secret_is_never_recorded(tmp_path):
    cwd = _workspace(tmp_path)
    _service(cwd.parent, "backend", BFF.replace("${OPENAI-API-KEY}", "sk-REAL-SECRET"))
    section = read_backend_config(search_from=cwd)
    assert "sk-REAL-SECRET" not in json.dumps(section)
    assert section["spring_ai"]["openai"]["api-key"] == "<redacted>"


def test_a_placeholder_is_kept_because_it_names_a_source_not_a_secret(tmp_path):
    cwd = _workspace(tmp_path)
    _service(cwd.parent, "backend", BFF)
    section = read_backend_config(search_from=cwd)
    assert section["spring_ai"]["openai"]["api-key"] == "${OPENAI-API-KEY}"


def test_backend_placeholders_are_never_resolved_against_the_eval_environment(tmp_path, monkeypatch):
    # resolving the backend's ${...} here would invent a value the backend never
    # saw, and could leak the eval pod's environment into the artifact
    monkeypatch.setenv("OPENAI_MODEL_NAME", "leaked-from-eval-env")
    cwd = _workspace(tmp_path)
    _service(cwd.parent, "backend", BFF.replace("model: gpt-5.2", "model: ${OPENAI_MODEL_NAME}"))
    section = read_backend_config(search_from=cwd)
    assert section["model"] == "${OPENAI_MODEL_NAME}"


# --- profiles ----------------------------------------------------------------

def test_profile_overlays_are_applied_only_when_asked(tmp_path):
    cwd = _workspace(tmp_path)
    path = _service(cwd.parent, "backend", BFF)
    path.with_name("application-local.yaml").write_text(
        "spring:\n  ai:\n    openai:\n      base-url: https://host.example/\n")

    plain = read_backend_config(search_from=cwd)
    assert "base_url" not in plain
    # the operator can still see the overlay exists without the harness guessing
    assert "application-local.yaml" in plain["profile_files_available"]

    overlaid = read_backend_config(search_from=cwd, profiles=["local"])
    assert overlaid["base_url"] == "https://host.example/"
    assert overlaid["files"] == ["application.yaml", "application-local.yaml"]
    assert overlaid["profiles_applied"] == ["local"]


def test_profile_gated_documents_in_one_file_are_honoured(tmp_path):
    cwd = _workspace(tmp_path)
    _service(cwd.parent, "backend", BFF + textwrap.dedent("""
        ---
        spring:
          config:
            activate:
              on-profile: prod
          ai:
            openai:
              chat:
                model: gpt-5.2-PTU
        """))
    assert read_backend_config(search_from=cwd)["model"] == "gpt-5.2"
    prod = read_backend_config(search_from=cwd, profiles=["prod"])
    assert prod["model"] == "gpt-5.2-PTU"
    assert prod["profiles_applied"] == ["prod"]


# --- robustness --------------------------------------------------------------

@pytest.mark.parametrize("body", ["{{{ not yaml", "spring:\n  ai:\n"])
def test_unreadable_or_empty_config_never_fails_a_run(tmp_path, body):
    cwd = _workspace(tmp_path)
    _service(cwd.parent, "backend", body)
    section = read_backend_config(search_from=cwd)
    assert "model" not in section
    assert section.get("error")


def test_the_section_is_independent_of_the_observed_backend_block(tmp_path):
    """The whole point of a separate section (E19).

    On 2026-08-25 the file declared gpt-5.2 while the meter reported gpt-5.5 over
    89 real calls. Merging the two would have hidden the drift; side by side, the
    bundle shows both and the disagreement is legible.
    """
    from argparse import Namespace

    from agent_evals.cli import _backend_params

    cwd = _workspace(tmp_path)
    _service(cwd.parent, "backend", BFF)
    declared_by_file = read_backend_config(search_from=cwd)
    observed = _backend_params({}, Namespace(model=None, deployment=None,
                                             reasoning_effort=None, api_version=None),
                               {"model": "gpt-5.5", "probe": {}})
    assert declared_by_file["model"] == "gpt-5.2"
    assert observed["model"] == "gpt-5.5"
    assert "model_mismatch" not in observed  # the file never feeds the observed block


def test_several_backend_checkouts_side_by_side_are_flagged_not_silently_picked(tmp_path):
    # a restored/baseline copy of the same repo beside the working one is easy to
    # have and invisible in the artifact; alphabetical order must not decide it
    # unannounced
    cwd = _workspace(tmp_path)
    _service(cwd.parent, "backend", BFF)
    _service(cwd.parent, "backend-baseline", BFF.replace("gpt-5.2", "gpt-4o"))

    section = read_backend_config(search_from=cwd)
    assert section["model"] == "gpt-5.2"
    conflict = section["conflicting_candidates"]
    assert any("backend-baseline" in c["path"] for c in conflict)
    assert conflict[0]["model"] == "gpt-4o"   # says WHAT it disagreed on, not just where


def test_services_that_agree_are_not_reported_as_ambiguity(tmp_path):
    # a monorepo points every service at the same LLM; listing them all would
    # bury the one case that matters
    cwd = _workspace(tmp_path)
    _service(cwd.parent, "backend", BFF)
    _service(cwd.parent, "backend-other-service", BFF)
    assert "conflicting_candidates" not in read_backend_config(search_from=cwd)


def test_the_section_names_which_service_was_read(tmp_path):
    cwd = _workspace(tmp_path)
    _service(cwd.parent, "backend", BFF.replace("spring:\n  ai:",
                                                "spring:\n  application:\n    name: bff-service\n  ai:"))
    assert read_backend_config(search_from=cwd)["application_name"] == "bff-service"


def test_an_embedding_model_is_never_read_as_the_agents_llm(tmp_path):
    # a service that also configures embeddings has a second `model` key; reading
    # it would put text-embedding-3-large in the run's provenance
    cwd = _workspace(tmp_path)
    _service(cwd.parent, "backend", textwrap.dedent("""
        spring:
          ai:
            openai:
              chat:
                model: gpt-5.2
                reasoning-effort: none
              embedding:
                options:
                  model: text-embedding-3-large
                  deployment-name: text-embedding-3-large
        """))
    section = read_backend_config(search_from=cwd)
    assert section["model"] == "gpt-5.2"
    assert "text-embedding" not in json.dumps({k: v for k, v in section.items()
                                               if k != "spring_ai"})


def test_client_level_settings_beside_a_chat_block_are_still_read(tmp_path):
    cwd = _workspace(tmp_path)
    _service(cwd.parent, "backend", textwrap.dedent("""
        spring:
          ai:
            openai:
              base-url: https://host.example/
              timeout: 150s
              max-retries: 2
              chat:
                model: gpt-5.2
        """))
    section = read_backend_config(search_from=cwd)
    assert (section["base_url"], section["timeout"], section["max_retries"]) == (
        "https://host.example/", "150s", 2)


def test_differing_completeness_is_not_reported_as_a_conflict(tmp_path):
    # one service spelling out a deployment another leaves implicit is not a
    # different model, and reporting it would bury a real disagreement
    cwd = _workspace(tmp_path)
    _service(cwd.parent, "a-backend", BFF)
    _service(cwd.parent, "b-backend", BFF.replace(
        "model: gpt-5.2", "model: gpt-5.2\n            deployment-name: hr-chat-eu"))
    assert "conflicting_candidates" not in read_backend_config(search_from=cwd)
