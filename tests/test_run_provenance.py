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

from agent_evals.cli import (
    _backend_params,
    _backend_warnings,
    _judge_params,
    _observed_count,
    build_parser,
)
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


# --- observed backend (E19 fix 2): the actuator answers, not the operator ---


def test_probe_fields_land_in_params_and_are_stamped_observed():
    probe = {"model": "gpt-5.5", "response_model": "gpt-5.5-2026-04-01",
             "provider": "azure_openai", "reasoning_effort": "medium",
             "probe": {"metrics": {"url": "http://h/actuator/metrics/x", "calls": 128}}}
    backend = _backend_params({}, _args([]), probe)
    assert backend["model"] == "gpt-5.5"
    assert backend["provider"] == "azure_openai"
    assert backend["source"] == "observed"
    assert backend["field_source"]["reasoning_effort"] == "observed"
    assert backend["probe"]["metrics"]["calls"] == 128


def test_the_block_says_per_field_which_half_answered():
    # the meter names the model; it structurally cannot carry reasoning effort,
    # so that half stays the operator's word and must not be dressed up as read.
    target = {"model": {"name": "gpt-5.5", "reasoning_effort": "high"}}
    backend = _backend_params(target, _args([]), {"model": "gpt-5.5", "probe": {}})
    assert backend["source"] == "mixed"
    assert backend["field_source"] == {"model": "observed", "reasoning_effort": "declared"}


def test_observed_beats_declared_and_the_disagreement_survives():
    # a declaration that does not match the running pod is the exact failure
    # E19 exists to catch, so it must be visible in the bundle, not resolved away
    target = {"model": {"name": "gpt-5.5"}}
    backend = _backend_params(target, _args([]), {"model": "gpt-4o", "probe": {}})
    assert backend["model"] == "gpt-4o"
    assert backend["declared_model"] == "gpt-5.5"
    assert backend["model_mismatch"] is True


def test_a_declaration_matching_any_accumulated_tag_value_is_not_a_mismatch():
    # tags accumulate since boot, so a failover leaves two values; the operator
    # declaring either one is telling the truth
    backend = _backend_params({"model": {"name": "gpt-4o"}}, _args([]),
                              {"model": ["gpt-4o", "gpt-5.5"], "probe": {}})
    assert backend["model"] == ["gpt-4o", "gpt-5.5"]
    assert "model_mismatch" not in backend


def test_a_mismatch_warns_and_names_both_values():
    backend = _backend_params({"model": {"name": "gpt-5.5"}}, _args([]),
                              {"model": "gpt-4o", "probe": {}})
    warning = "\n".join(_backend_warnings(backend))
    assert "gpt-5.5" in warning and "gpt-4o" in warning
    assert "OBSERVED" in warning


def test_a_failed_probe_records_that_the_harness_asked():
    # silence is indistinguishable from "never tried"; the bundle must show the
    # attempt and what came back
    probe = {"probe": {"metrics": {"url": "http://h/actuator/metrics/x",
                                   "error": "HTTP 404 (endpoint not exposed, or no LLM call yet)"}}}
    backend = _backend_params({}, _args([]), probe)
    assert backend["source"] == "unknown"
    assert "404" in backend["probe"]["metrics"]["error"]
    assert "will not name the model" in "\n".join(_backend_warnings(backend))


def test_a_failed_probe_does_not_discard_the_declaration():
    target = {"model": {"name": "gpt-5.5", "reasoning_effort": "high"}}
    backend = _backend_params(target, _args([]), {"probe": {"metrics": {"error": "HTTP 403"}}})
    assert backend["model"] == "gpt-5.5"
    assert backend["source"] == "declared"
    assert _backend_warnings(backend) == []


def test_no_probe_keeps_the_pre_existing_declared_shape():
    # back-compat: a run against a backend with no actuator writes exactly what
    # it wrote before this fix, with no probe/field_source noise
    backend = _backend_params({"model": {"name": "gpt-5.5"}}, _args([]), None)
    assert backend == {"model": "gpt-5.5", "source": "declared"}


def test_params_json_can_be_rewritten_after_the_run():
    # the meter does not exist until the backend's first LLM call, so the model
    # is often only readable once the eval's own turns have registered it
    with tempfile.TemporaryDirectory() as tmp:
        sink = JsonlSink(out_dir=tmp)
        sink.start_run(name="it", params={"suite": "hr", "backend": {"source": "unknown"}})
        sink.end_run()
        sink.update_params({"suite": "hr", "backend": {"model": "gpt-5.5",
                                                       "source": "observed"}})
        written = json.loads((Path(tmp) / "it" / "params.json").read_text())
    assert written["backend"] == {"model": "gpt-5.5", "source": "observed"}


def test_the_refresh_never_downgrades_what_the_first_probe_observed():
    observed = _backend_params({}, _args([]), {"model": "gpt-5.5", "probe": {}})
    failed = _backend_params({}, _args([]), {"probe": {"metrics": {"error": "HTTP 500"}}})
    assert _observed_count(observed) == 1
    assert _observed_count(failed) == 0


def test_a_cold_meter_is_re_probed_after_the_run_and_params_rewritten(monkeypatch):
    """The whole point of the post-run probe (E19).

    A freshly booted backend has never called an LLM, so its GenAI meter does
    not exist and the run STARTS unable to name its own model. The eval's turns
    register the meter, so the same question answered at the end succeeds.
    """
    from agent_evals import cli

    answers = iter([
        {"probe": {"metrics": {"url": "http://h/actuator/metrics/x", "error": "HTTP 404"}}},
        {"model": "gpt-5.5", "provider": "azure_openai",
         "probe": {"metrics": {"url": "http://h/actuator/metrics/x", "calls": 4}}},
    ])
    monkeypatch.setattr(cli, "_probe_backend", lambda *a, **k: next(answers))

    args, target = _args([]), {}
    before = cli._backend_params(target, args, cli._probe_backend(target, None))
    assert before["source"] == "unknown"

    with tempfile.TemporaryDirectory() as tmp:
        sink = JsonlSink(out_dir=tmp)
        params = {"suite": "hr", "backend": before}
        sink.start_run(name="it", params=params)
        sink.end_run()
        cli._refresh_backend_params(target, args, None, params, sink, before=before)
        written = json.loads((Path(tmp) / "it" / "params.json").read_text())

    assert written["backend"]["model"] == "gpt-5.5"
    assert written["backend"]["source"] == "observed"
    assert written["backend"]["probe"]["metrics"]["calls"] == 4


def test_a_transient_failure_on_the_second_probe_cannot_erase_the_first(monkeypatch):
    from agent_evals import cli

    answers = iter([
        {"model": "gpt-5.5", "probe": {"metrics": {"calls": 2}}},
        {"probe": {"metrics": {"error": "HTTP 503"}}},
    ])
    monkeypatch.setattr(cli, "_probe_backend", lambda *a, **k: next(answers))

    args, target = _args([]), {}
    before = cli._backend_params(target, args, cli._probe_backend(target, None))

    with tempfile.TemporaryDirectory() as tmp:
        sink = JsonlSink(out_dir=tmp)
        params = {"suite": "hr", "backend": before}
        sink.start_run(name="it", params=params)
        sink.end_run()
        cli._refresh_backend_params(target, args, None, params, sink, before=before)
        written = json.loads((Path(tmp) / "it" / "params.json").read_text())

    assert written["backend"]["model"] == "gpt-5.5"
