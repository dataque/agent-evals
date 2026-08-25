"""Command-line entry point: ``agent-evals run`` / ``rescore`` / ``list-metrics``.

Wires the neutral pieces together: load a target + suite, build the transport +
identity, select scorers, bind judges, pick a sink, run, print a summary.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import yaml

import agent_evals

from .core.runner import Runner
from .appconfig import read_backend_config
from .datasets import load_suite, suite_fingerprint
from .datasets.facts import derive_hr_facts
from .envfile import expand_env, load_dotenv
from .judges import apply_per_metric_judges, build_judge, describe_judge
from .scorers import build_registry, get_scorers
from .sinks import JsonlSink, MlflowSink
from .transport import (
    AgUiSseTransport,
    Identity,
    LocalJwtMinter,
    Session,
    SessionState,
    StaticTokenProvider,
    probe_backend,
)
from .transport.actuator import OBSERVED_ONLY_DETAIL


def _default_config_path() -> Path:
    return Path(agent_evals.__file__).parent / "config" / "targets.yaml"


def _load_config(path: str | None) -> dict:
    load_dotenv()  # .env supplies ${VAR} values referenced in the config
    p = Path(path) if path else _default_config_path()
    return expand_env(yaml.safe_load(p.read_text()) or {})


def _tls_verify(target: dict) -> bool | str:
    """The target's TLS policy, shared by the SSE transport and the model probe
    so both reach the same backend under the same trust rules."""
    tls = target.get("tls", {}) or {}
    if tls.get("use_truststore"):
        try:
            import truststore

            truststore.inject_into_ssl()
        except Exception as exc:  # missing extra
            raise SystemExit("tls.use_truststore set but truststore is unavailable; "
                             "run `pip install truststore`") from exc
    if tls.get("insecure"):
        return False
    if tls.get("ca_bundle"):
        return tls["ca_bundle"]
    return True


def _build_transport(target: dict, persist_dir: str | None) -> AgUiSseTransport:
    transport = target.get("transport", "agui_sse")
    if transport != "agui_sse":
        raise SystemExit(f"unsupported transport: {transport!r} (only agui_sse in v1)")
    base_url = (target.get("base_url") or "").strip()
    if not base_url or "REPLACE-ME" in base_url:
        raise SystemExit("target base_url is empty/unset — set the target's URL env var in a "
                         ".env file (e.g. AGENT_EVALS_DEVPOD_BASE_URL; copy .env.example), or "
                         "set base_url directly in the target config.")
    verify = _tls_verify(target)
    return AgUiSseTransport(
        base_url,
        persist_dir=persist_dir,
        verify=verify,
        create_thread=target.get("create_thread", True),
        graphql_url=target.get("graphql_url"),
    )


def _build_identity(target: dict) -> Identity:
    auth = target.get("auth", {}) or {}
    atype = auth.get("type", "local_jwt")
    if atype == "local_jwt":
        user_login_id = (auth.get("user_login_id") or "").strip()
        if not user_login_id:
            raise SystemExit("local_jwt target needs a user login id — set "
                             "AGENT_EVALS_USER_LOGIN_ID in a .env file (copy .env.example), "
                             "or set auth.user_login_id in the target config.")
        scopes = auth.get("scopes")
        if scopes is None and auth.get("scope"):
            scopes = [auth["scope"]]
        return Identity(
            user_id=user_login_id,
            token_provider=LocalJwtMinter(
                user_login_id,
                user_claim=auth.get("user_claim"),
                roles=auth.get("roles"),
                scopes=scopes,
            ),
        )
    if atype == "static":
        env = auth.get("token_env", "AGENT_EVALS_TOKEN")
        token = os.getenv(env)
        if not token:
            raise SystemExit(f"static auth requires env var {env!r} to be set")
        return Identity(user_id=auth.get("user_id", "user"), token_provider=StaticTokenProvider(token))
    raise SystemExit(f"unsupported auth type: {atype!r}")


def _judge_params(scorers, default_judge) -> tuple[dict, dict]:
    """The judge side of a run's provenance: which model scored which metric (E19).

    ``judge: azure_openai`` names a backend, not a model, and the backend reads
    its deployment from the environment, so two runs can carry the same judge
    name and be scored by different models. Read from the BOUND scorers, so a
    ``per_metric`` entry that never applied (metric not selected, or not judged)
    is absent rather than advertised.
    """
    per_metric = {}
    for scorer in scorers:
        judge = getattr(scorer, "judge", None)
        if judge is not None and judge is not default_judge:
            per_metric[scorer.spec.metric] = describe_judge(judge)
    return describe_judge(default_judge), per_metric


_BACKEND_FIELDS = ("model", "deployment", "reasoning_effort", "api_version")
# No operator can declare these: they exist only if the backend reports them.
_OBSERVED_ONLY_FIELDS = ("response_model", "provider")


def _agrees(declared: str, observed: object) -> bool:
    """Whether a declaration is consistent with what the backend reported.

    An accumulated actuator tag can hold several values (a failover, or
    per-agent models); the declaration matches if it is among them.
    """
    values = observed if isinstance(observed, list) else [observed]
    return any(declared == str(v) for v in values)


def _probe_backend(target: dict, identity: Identity, *, timeout_s: float = 5.0) -> dict | None:
    """Ask the target's actuator what LLM it is running.

    Returns ``None`` when there is nothing to ask (no ``base_url``), and
    otherwise whatever the probe found, including a bare ``probe`` block
    recording that the harness asked and was refused. Never raises: a run must
    not fail because a metrics endpoint is absent or locked down.
    """
    base_url = (target.get("base_url") or "").strip()
    if not base_url or "REPLACE-ME" in base_url:
        return None
    try:
        token = identity.token_provider.get_token()
    except Exception:  # noqa: BLE001 - an unauthenticated probe is still worth trying
        token = None
    return probe_backend(base_url, token=token, verify=_tls_verify(target), timeout_s=timeout_s)


def _backend_params(target: dict, args: argparse.Namespace, probe: dict | None = None) -> dict:
    """What the system under test was running, for ``params.json`` (E19).

    Two sources, and the block says per field which one answered.

    *Observed* comes from the backend's Spring Boot actuator: the GenAI meter
    ``gen_ai.client.operation`` for model identity, the config endpoints for the
    request options a Micrometer meter structurally cannot carry (reasoning
    effort, API version, deployment). *Declared* is the operator's word, from
    the target's ``model:`` block (which may itself come from ``.env``) with CLI
    flags taking precedence over it.

    Observed OUTRANKS declared, because it is a record of calls that actually
    happened. When the two disagree the observed value is recorded and the
    declaration is preserved beside it as ``declared_<field>`` with a
    ``<field>_mismatch`` flag: a mismatch between what an operator believes and
    what the pod is running is precisely the failure E19 exists to catch, so it
    must survive into the bundle rather than being silently resolved.

    Empty values are dropped so a run that declared nothing and observed nothing
    is visibly empty rather than full of blank strings.
    """
    cfg = target.get("model") or {}
    # the config block reads `model: {name: ...}`; params.json keys it as `model`
    from_cfg = {("model" if k == "name" else k): v for k, v in cfg.items()
                if k in _BACKEND_FIELDS or k == "name"}
    overrides = {
        "model": args.model,
        "deployment": args.deployment,
        "reasoning_effort": args.reasoning_effort,
        "api_version": args.api_version,
    }
    merged = {**from_cfg, **{k: v for k, v in overrides.items() if v}}
    declared = {}
    for key in _BACKEND_FIELDS:
        value = str(merged.get(key) or "").strip()
        if value:
            declared[key] = value

    observed = {k: v for k, v in (probe or {}).items() if k != "probe" and v}

    backend: dict = {}
    field_source: dict = {}
    for key in (*_BACKEND_FIELDS, *_OBSERVED_ONLY_FIELDS):
        seen, said = observed.get(key), declared.get(key)
        if seen:
            backend[key] = seen
            field_source[key] = "observed"
            if said and not _agrees(said, seen):
                backend[f"declared_{key}"] = said
                backend[f"{key}_mismatch"] = True
        elif said:
            backend[key] = said
            field_source[key] = "declared"

    # Observed-only detail: the wider chat-option set, the active profiles and
    # the property source that won each field. No operator can declare these, so
    # they are carried across whole rather than compared against a declaration.
    for key in OBSERVED_ONLY_DETAIL:
        value = observed.get(key)
        if value:
            backend[key] = value

    if not backend and not probe:
        return {}
    kinds = set(field_source.values())
    if any(backend.get(key) for key in OBSERVED_ONLY_DETAIL):
        kinds.add("observed")
    if not kinds:
        # the harness asked and learned nothing, which is itself worth recording
        backend["source"] = "unknown"
    elif kinds == {"observed"}:
        backend["source"] = "observed"
    elif kinds == {"declared"}:
        backend["source"] = "declared"
    else:
        backend["source"] = "mixed"
    if probe:
        # only meaningful once a probe has run; without one every field is
        # declared and the summary `source` already says so.
        backend["field_source"] = field_source
        if probe.get("probe"):
            backend["probe"] = probe["probe"]
    return backend


def _backend_config_params(target: dict) -> dict:
    """The backend's own ``application.yaml``, as its own ``params.json`` section.

    Reads no environment variable and requires no ``.env`` entry: with nothing
    configured the file is discovered next to the eval's working directory, which
    is where it sits when the eval runs beside the backend. The optional
    ``model_config`` block on the target only narrows that search, and
    ``enabled: false`` turns the whole thing off.
    """
    cfg = target.get("model_config") or {}
    if cfg.get("enabled") is False:
        return {}
    return read_backend_config(path=cfg.get("path"),
                               backend_root=cfg.get("root"),
                               service=cfg.get("service"),
                               profiles=cfg.get("profiles"))


def _backend_warnings(backend: dict) -> list[str]:
    """Operator-facing warnings about a run's model provenance (E19)."""
    warnings = []
    for key in _BACKEND_FIELDS:
        if backend.get(f"{key}_mismatch"):
            warnings.append(
                f"WARNING: declared {key} {backend[f'declared_{key}']!r} does not match the "
                f"backend's own {backend[key]!r} (E19). The OBSERVED value is what has been "
                "recorded; fix the declaration or the deployment before citing this run.")
    if not backend.get("model"):
        warnings.append(
            "WARNING: this run's artifacts will not name the model that produced them (E19): "
            "nothing was declared and the backend's actuator did not report one. Pass "
            "--model/--reasoning-effort, set a model: block on the target in targets.yaml, or "
            "expose /actuator/metrics/gen_ai.client.operation on the backend.")
    return warnings


def cmd_run(args: argparse.Namespace) -> int:
    cfg = _load_config(args.config)
    targets = cfg.get("targets", {})
    if args.target not in targets:
        raise SystemExit(f"unknown target {args.target!r}; available: {sorted(targets)}")
    target = targets[args.target]

    transport = _build_transport(target, persist_dir=args.persist)
    identity = _build_identity(target)
    timeout_s = float(args.timeout) if args.timeout else float(target.get("timeout_s", 120))

    scorers = get_scorers(args.metrics)
    judge_cfg = cfg.get("judge", {}) or {}
    default_name = args.judge or judge_cfg.get("default", "heuristic")
    default_judge = build_judge(default_name)
    apply_per_metric_judges(scorers, default=default_judge, per_metric=judge_cfg.get("per_metric"))

    scoring_cfg = cfg.get("scoring", {}) or {}
    sink = (
        MlflowSink(experiment=args.experiment, tracking_uri=args.tracking_uri)
        if args.sink == "mlflow"
        else JsonlSink(out_dir=args.out)
    )

    cases = load_suite(args.suite)
    if args.limit:
        cases = cases[: args.limit]

    def session_factory(case):
        return Session(transport, identity, state=SessionState(), timeout_s=timeout_s)

    # Data-facts are derived per-run, so the same suite is portable across
    # environments (dev/UAT/prod) with no frozen/seeded data (#32). A case whose
    # `requires:` precondition isn't met in this env is skipped and reported.
    run_config = {**scoring_cfg, "derive_facts": derive_hr_facts}
    runner = Runner(session_factory=session_factory, scorers=scorers, sink=sink,
                    judge=default_judge, config=run_config)
    run_name = args.run_name or f"{args.suite}-{args.target}-{time.strftime('%Y%m%d-%H%M%S')}"
    # The GenAI meter is registered lazily, so this probe legitimately finds
    # nothing against a freshly booted pod. Ask anyway: on a warm backend it
    # makes the model readable in params.json from the moment the run starts,
    # and the post-run probe below covers the cold case.
    backend = _backend_params(target, args, _probe_backend(target, identity))
    judge_model, judge_per_metric = _judge_params(scorers, default_judge)
    params = {
        "suite": args.suite, "target": args.target, "metrics": args.metrics,
        "judge": default_name, "sink": args.sink, "version": agent_evals.__version__,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        # The dataset is untracked, so without this a run cannot be shown to
        # reflect the suite in the repo (E5).
        "dataset": suite_fingerprint(args.suite),
        # Which LLM the system under test was running, and how hard it was told
        # to think. Without it two runs an hour apart are indistinguishable in
        # their artifacts and no model A/B is citable (E19).
        "backend": backend,
        # What the backend's own application.yaml DECLARES, kept as its own
        # section rather than merged into `backend` above: that one records what
        # the running process did, this one records what the build was
        # configured to do, and the two disagreeing is the finding (E19).
        "backend_config": _backend_config_params(target),
        # And which LLM did the judging: a judged score is only comparable across
        # runs that were scored by the same judge model (E19).
        "judge_model": judge_model,
        **({"judge_per_metric": judge_per_metric} if judge_per_metric else {}),
    }
    for warning in _backend_warnings(backend):
        print(warning + "\n")
    judge_label = default_name + (f"/{judge_model['model']}" if judge_model.get("model") else "")
    print(f"Running {len(cases)} cases [{args.suite} → {args.target}] "
          f"with {len(scorers)} scorers, judge={judge_label}, sink={args.sink}"
          f"{', model=' + str(backend['model']) if backend.get('model') else ''} ...")
    report = runner.run(cases, run_name=run_name, params=params)
    _refresh_backend_params(target, args, identity, params, sink, before=backend)
    _print_summary(report, run_name, args)
    return 0


def _observed_count(backend: dict) -> int:
    return sum(1 for kind in (backend.get("field_source") or {}).values() if kind == "observed")


def _refresh_backend_params(target: dict, args: argparse.Namespace, identity: Identity,
                            params: dict, sink, *, before: dict) -> None:
    """Re-probe the backend after the run and rewrite ``params.json`` (E19).

    This is the probe that actually matters. The GenAI meter does not exist until
    the backend's first LLM call, so a cold pod reports nothing at run start and
    reports the model by the time the run ends; the eval's own turns are what
    registered it.

    The refresh only ever replaces the block with one that knows at least as
    much, so a transient failure on the second probe cannot erase what the first
    one observed.
    """
    after = _backend_params(target, args, _probe_backend(target, identity))
    if after == before or _observed_count(after) < _observed_count(before):
        return
    params["backend"] = after
    try:
        sink.update_params(params)
    except Exception as exc:  # noqa: BLE001 - the run is already complete
        print(f"NOTE: could not rewrite the run's params after the backend re-probe: {exc}\n")
        return
    if after.get("model") and not before.get("model"):
        print(f"Backend model observed after the run: {after['model']} "
              f"(provider={after.get('provider', 'unknown')}).")
    for warning in _backend_warnings(after):
        if warning not in _backend_warnings(before):
            print(warning + "\n")


def cmd_rescore(args: argparse.Namespace) -> int:
    """Re-score a frozen run through the current scorers, with no live agent.

    This is how a scorer or calibration change is verified: the transcripts are
    fixed, so any movement in a deterministic metric is attributable to the code
    change. Judged metrics still carry judge variance and must be read on row
    counts and direction, not exact means.
    """
    from .replay import ReplayError, build_replay_factory, load_recorded_runs, reconcile

    src = Path(args.run)
    if not src.is_dir():
        raise SystemExit(f"run directory not found: {src}")
    try:
        src_params = json.loads((src / "params.json").read_text())
    except FileNotFoundError:
        src_params = {}

    suite = args.suite or src_params.get("suite") or "hr"
    metrics = args.metrics or src_params.get("metrics") or "all"

    cfg = _load_config(args.config)
    judge_cfg = cfg.get("judge", {}) or {}
    scoring_cfg = cfg.get("scoring", {}) or {}

    try:
        records_by_case = load_recorded_runs(src)
    except ReplayError as exc:
        raise SystemExit(str(exc)) from exc

    cases = load_suite(suite)
    replayable, missing_rec, missing_suite = reconcile(cases, records_by_case)
    if missing_rec or missing_suite:
        lines = ["the suite and the recording do not match:"]
        if missing_rec:
            lines.append(f"  in the suite but never recorded ({len(missing_rec)}): "
                         f"{', '.join(sorted(missing_rec))}")
        if missing_suite:
            lines.append(f"  recorded but no longer in the suite ({len(missing_suite)}): "
                         f"{', '.join(sorted(missing_suite))}")
        if not args.allow_partial:
            lines.append("Re-capture live, or pass --allow-partial to score only the "
                         "cases that still match.")
            raise SystemExit("\n".join(lines))
        for line in lines:
            print(line)
        print(f"--allow-partial: scoring {len(replayable)} of {len(cases)} cases.\n")
    if not replayable:
        raise SystemExit("nothing to replay: no case in the suite appears in the recording")

    fingerprint = suite_fingerprint(suite)
    recorded_fp = src_params.get("dataset") or {}
    if recorded_fp and recorded_fp.get("digest") != fingerprint["digest"]:
        print(f"WARNING: dataset digest differs from the recording "
              f"({recorded_fp.get('digest')} recorded, {fingerprint['digest']} now). "
              "Goldens have changed since this run; scores are being recomputed "
              "against the CURRENT dataset.\n")
    elif not recorded_fp:
        print("NOTE: this run predates dataset fingerprinting, so the goldens it was "
              "originally scored against cannot be confirmed (E5).\n")

    scorers = get_scorers(metrics)
    default_name = args.judge or judge_cfg.get("default", "heuristic")
    default_judge = build_judge(default_name)
    apply_per_metric_judges(scorers, default=default_judge, per_metric=judge_cfg.get("per_metric"))

    sink = (
        MlflowSink(experiment=args.experiment, tracking_uri=args.tracking_uri)
        if args.sink == "mlflow"
        else JsonlSink(out_dir=args.out)
    )
    run_config = {**scoring_cfg, "derive_facts": derive_hr_facts}
    runner = Runner(session_factory=build_replay_factory(records_by_case), scorers=scorers,
                    sink=sink, judge=default_judge, config=run_config)
    run_name = args.run_name or f"{src.name}-rescore-{time.strftime('%Y%m%d-%H%M%S')}"
    judge_model, judge_per_metric = _judge_params(scorers, default_judge)
    params = {
        # Loud, machine-readable provenance: a replayed summary must never be
        # mistaken for a live run.
        "replay": True,
        "replay_source": str(src),
        "replay_source_started_at": src_params.get("started_at"),
        "replay_partial": bool(missing_rec or missing_suite),
        "suite": suite, "target": src_params.get("target"), "metrics": metrics,
        "judge": default_name, "sink": args.sink, "version": agent_evals.__version__,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "dataset": fingerprint,
        # Carried from the source run: a rescore re-reads frozen transcripts, so
        # the backend that produced them is the source run's, never this one's
        # (E19). Empty when the source predates backend recording.
        "backend": src_params.get("backend") or {},
        # The JUDGE, by contrast, runs live here, so this describes THIS
        # invocation. The source run's judge is one field of the comparison a
        # rescore exists to make, and is kept beside it.
        "judge_model": judge_model,
        **({"judge_per_metric": judge_per_metric} if judge_per_metric else {}),
        "replay_source_judge_model": src_params.get("judge_model") or {},
    }
    judge_label = default_name + (f"/{judge_model['model']}" if judge_model.get("model") else "")
    print(f"Replaying {len(replayable)} cases from {src} "
          f"with {len(scorers)} scorers, judge={judge_label} ...")
    try:
        report = runner.run(replayable, run_name=run_name, params=params)
    except ReplayError as exc:
        raise SystemExit(f"replay aborted: {exc}") from exc
    _print_summary(report, run_name, args)
    return 0


def _print_summary(report, run_name: str, args: argparse.Namespace) -> None:
    agg = report.aggregates
    print(f"\n=== {run_name}: {len(report.case_results)} cases ===")
    quality = {k: v for k, v in agg.items()
               if k.endswith(".mean") and not k.startswith(("tokens", "latency"))}
    if quality:
        print("Quality (mean 0..1):")
        for k in sorted(quality):
            print(f"  {k[:-5]:<34} {quality[k]:.3f}")
    passes = {k: v for k, v in agg.items() if k.endswith(".pass_rate")}
    if passes:
        print("Pass rates:")
        for k in sorted(passes):
            print(f"  {k[:-10]:<34} {passes[k]:.0%}")
    print("Operational:")
    for k in ("latency.ttft_ms.p50", "latency.ttft_ms.p95", "latency.total_ms.p50",
              "latency.total_ms.p95", "latency.abort_rate", "tokens.total.sum",
              "tokens.total.mean", "tokens.estimated_fraction"):
        if k in agg:
            print(f"  {k:<34} {agg[k]:.3f}")
    skipped = agg.get("skipped_cases") or []
    if skipped:
        print(f"Skipped — precondition not met in this environment ({len(skipped)}):")
        for s in skipped:
            print(f"  {s['case_id']:<40} requires {s.get('requires')}")
    boundary = agg.get("forbidden_route_violations") or []
    if boundary:
        print(f"\nPERSONA BOUNDARY BREACH ({len(boundary)}) — this suite's persona reached "
              "another persona's agent:")
        for b in boundary:
            print(f"  {b['case_id']} t{b.get('turn_index')}: {b.get('routes')}")
    violations = agg.get("route_violations") or []
    if violations:
        print(f"\nROUTE VIOLATIONS ({len(violations)}) — a turn reached an agent outside its envelope:")
        for v in violations:
            print(f"  {v['case_id']} t{v.get('turn_index')}: "
                  f"{v.get('outside_envelope')} not in {v.get('expected_routes')}")
    undrivable = agg.get("precondition_never_derived_cases") or []
    if undrivable:
        print(f"\nScored despite an underivable precondition ({len(undrivable)}) — "
              "the fact-bearing tool never ran:")
        for u in undrivable:
            print(f"  {u['case_id']:<40} never derived {u.get('never_derived')}")
    if args.sink == "jsonl":
        print(f"\nWrote results to {Path(args.out) / run_name}/")


def cmd_ingest_feedback(args: argparse.Namespace) -> int:
    from .feedback import ingest

    sink = (
        MlflowSink(experiment=args.experiment, tracking_uri=args.tracking_uri)
        if args.sink == "mlflow"
        else JsonlSink(out_dir=args.out)
    )
    agg = ingest(args.input, sink, run_name=args.run_name or "user-feedback")
    print("Ingested user feedback:")
    for k in sorted(agg):
        print(f"  {k:<34} {agg[k]:.3f}")
    return 0


def cmd_list_metrics(_args: argparse.Namespace) -> int:
    reg = build_registry()
    print(f"{len(reg)} metrics registered:\n")
    print(f"{'#':<4}{'metric':<32}{'family':<14}{'scope':<7}{'judge':<7}{'golden'}")
    for _metric, s in sorted(reg.items(), key=lambda kv: kv[1].spec.number):
        sp = s.spec
        print(f"{sp.number:<4}{sp.metric:<32}{sp.family.value:<14}{sp.turn_scope.value:<7}"
              f"{str(sp.needs_judge):<7}{sp.needs_golden}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agent-evals", description="Evaluate an agentic chat system.")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="Run an eval suite against a target.")
    r.add_argument("--target", default="local", help="target name from the config")
    r.add_argument("--suite", default="hr", help="bundled suite name or path to a suite file/dir")
    r.add_argument("--metrics", default="all",
                   help="all | primary | secondary | <family> | comma-separated metric ids")
    r.add_argument("--sink", choices=["jsonl", "mlflow"], default="jsonl")
    r.add_argument("--judge", default=None, help="override the default judge backend")
    r.add_argument("--config", default=None, help="path to a targets.yaml (defaults to bundled)")
    r.add_argument("--out", default="eval-runs", help="output dir for the jsonl sink")
    r.add_argument("--experiment", default="agent-evals", help="MLflow experiment name")
    r.add_argument("--tracking-uri", default=None, help="MLflow tracking URI")
    r.add_argument("--persist", default=None, help="dir to persist raw SSE transcripts")
    r.add_argument("--run-name", default=None)
    r.add_argument("--timeout", type=float, default=None, help="per-turn timeout (s)")
    r.add_argument("--model", default=None,
                   help="LLM the system under test is running (e.g. gpt-5.5); recorded in "
                        "params.json (E19). Overrides the target's model.name.")
    r.add_argument("--deployment", default=None,
                   help="backend's deployment/endpoint name for that model")
    r.add_argument("--reasoning-effort", default=None,
                   help="backend's reasoning-effort setting, e.g. low|medium|high")
    r.add_argument("--api-version", default=None, help="backend's LLM API version")
    r.add_argument("--limit", type=int, default=None, help="only run the first N cases")
    r.set_defaults(func=cmd_run)

    rs = sub.add_parser("rescore", help="Re-score a frozen run's transcripts (no live agent).")
    rs.add_argument("--run", required=True, help="path to a jsonl run dir, e.g. eval-runs/eval_run12")
    rs.add_argument("--metrics", default=None, help="defaults to the recorded run's selection")
    rs.add_argument("--suite", default=None, help="defaults to the recorded run's suite")
    rs.add_argument("--sink", choices=["jsonl", "mlflow"], default="jsonl")
    rs.add_argument("--judge", default=None, help="override the default judge backend")
    rs.add_argument("--config", default=None, help="path to a targets.yaml (defaults to bundled)")
    rs.add_argument("--out", default="eval-runs", help="output dir for the jsonl sink")
    rs.add_argument("--experiment", default="agent-evals", help="MLflow experiment name")
    rs.add_argument("--tracking-uri", default=None, help="MLflow tracking URI")
    rs.add_argument("--run-name", default=None)
    rs.add_argument("--allow-partial", action="store_true",
                    help="score only the cases that still match the recording, instead of "
                         "refusing when the suite has drifted")
    rs.set_defaults(func=cmd_rescore)

    f = sub.add_parser("ingest-feedback", help="Aggregate production user feedback (#23) into a sink.")
    f.add_argument("--input", required=True, help="path to feedback .jsonl/.json")
    f.add_argument("--sink", choices=["jsonl", "mlflow"], default="jsonl")
    f.add_argument("--out", default="eval-runs")
    f.add_argument("--experiment", default="agent-evals")
    f.add_argument("--tracking-uri", default=None)
    f.add_argument("--run-name", default=None)
    f.set_defaults(func=cmd_ingest_feedback)

    m = sub.add_parser("list-metrics", help="List registered metrics.")
    m.set_defaults(func=cmd_list_metrics)
    return p


def main(argv: list[str] | None = None) -> int:
    load_dotenv()  # make .env values (judge creds, etc.) available to all commands
    args = build_parser().parse_args(argv)
    return args.func(args)
