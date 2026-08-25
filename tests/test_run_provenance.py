"""E19: a run bundle must name the models that produced and scored it.

`params.json` used to record suite/target/metrics/judge/sink/version/dataset and
nothing about the system under test's LLM, which made two runs an hour apart
indistinguishable in their artifacts and any model A/B uncitable. `judge` named
only a backend (`azure_openai`), so the judge model could change under a run
series without leaving a trace either.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from agent_evals.cli import _backend_params, _judge_params, build_parser
from agent_evals.judges import apply_per_metric_judges, build_judge, describe_judge
from agent_evals.scorers import get_scorers
from agent_evals.sinks import JsonlSink


def _args(argv: list[str]):
    return build_parser().parse_args(["run", *argv])


def test_target_model_block_lands_in_params():
    target = {"model": {"name": "gpt-5.5", "deployment": "hr-chat-eu",
                        "reasoning_effort": "medium", "api_version": "2024-10-21"}}
    assert _backend_params(target, _args([])) == {
        "model": "gpt-5.5", "deployment": "hr-chat-eu",
        "reasoning_effort": "medium", "api_version": "2024-10-21",
        "source": "declared",
    }


def test_cli_flags_override_the_target_block():
    target = {"model": {"name": "gpt-5.2", "reasoning_effort": "low"}}
    backend = _backend_params(target, _args(["--model", "gpt-5.5", "--reasoning-effort", "high"]))
    assert backend["model"] == "gpt-5.5"
    assert backend["reasoning_effort"] == "high"


def test_flags_alone_are_enough():
    backend = _backend_params({}, _args(["--model", "gpt-5.5"]))
    assert backend == {"model": "gpt-5.5", "source": "declared"}


def test_unset_env_placeholders_are_dropped_not_recorded_blank():
    # targets.yaml defaults every field to '' when the .env var is unset; a run
    # that declared nothing must be visibly empty, not full of blank strings.
    target = {"model": {"name": "", "deployment": "  ", "reasoning_effort": None}}
    assert _backend_params(target, _args([])) == {}


def test_declared_source_is_stamped():
    # The backend does not announce its model, so the value is only as truthful
    # as the operator; the artifact must not imply it was observed.
    assert _backend_params({}, _args(["--model", "gpt-5.5"]))["source"] == "declared"


def test_provenance_blocks_are_written_to_params_json():
    params = {"suite": "hr",
              "backend": {"model": "gpt-5.5", "reasoning_effort": "high", "source": "declared"},
              "judge_model": {"backend": "azure_openai", "model": "gpt-5-eval",
                              "source": "observed"}}
    with tempfile.TemporaryDirectory() as tmp:
        JsonlSink(out_dir=tmp).start_run(name="it", params=params)
        written = json.loads((Path(tmp) / "it" / "params.json").read_text())
    assert written["backend"]["model"] == "gpt-5.5"
    assert written["backend"]["reasoning_effort"] == "high"
    assert written["judge_model"]["model"] == "gpt-5-eval"


# --- judge side (E19): `judge: azure_openai` names a backend, not a model ---


def test_judge_model_is_read_off_the_constructed_judge(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-5-eval")
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2025-04-01-preview")
    described = describe_judge(build_judge("azure_openai"))
    assert described == {"backend": "azure_openai", "model": "gpt-5-eval",
                         "temperature": 0.0, "max_tokens": 400,
                         "api_version": "2025-04-01-preview", "source": "observed"}


def test_judge_without_a_model_still_names_its_backend():
    # a heuristic-judged run must be legible as "no LLM scored this"
    assert describe_judge(build_judge("heuristic")) == {"backend": "heuristic",
                                                        "source": "observed"}


def test_describe_judge_never_reports_credentials(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "sk-secret-value")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://internal.example/openai")
    described = describe_judge(build_judge("azure_openai"))
    assert "sk-secret-value" not in json.dumps(described)
    assert not any("key" in k or "endpoint" in k for k in described)


def test_per_metric_judge_overrides_are_recorded_per_metric():
    default = build_judge("azure_openai")
    scorers = apply_per_metric_judges(get_scorers("all"), default=default,
                                      per_metric={"faithfulness": "heuristic"})
    judge_model, per_metric = _judge_params(scorers, default)
    assert judge_model["backend"] == "azure_openai"
    # only the metric that actually got a different judge is listed
    assert list(per_metric) == ["faithfulness"]
    assert per_metric["faithfulness"]["backend"] == "heuristic"


def test_no_per_metric_overrides_means_no_per_metric_block():
    default = build_judge("heuristic")
    scorers = apply_per_metric_judges(get_scorers("all"), default=default)
    _judge_model, per_metric = _judge_params(scorers, default)
    assert per_metric == {}


def test_azure_api_version_is_pinned_at_construction(monkeypatch):
    # the client is built lazily on first use; the version reported must be the
    # one it will use, not whatever the env says later.
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
    judge = build_judge("azure_openai")
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "changed-after-the-fact")
    assert describe_judge(judge)["api_version"] == "2024-10-21"
