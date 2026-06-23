"""Command-line entry point: ``agent-evals run`` / ``list-metrics``.

Wires the neutral pieces together: load a target + suite, build the transport +
identity, select scorers, bind judges, pick a sink, run, print a summary.
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

import yaml

import agent_evals

from .core.runner import Runner
from .datasets import load_suite
from .envfile import expand_env, load_dotenv
from .judges import apply_per_metric_judges, build_judge
from .scorers import build_registry, get_scorers
from .sinks import JsonlSink, MlflowSink
from .transport import (
    AgUiSseTransport,
    Identity,
    LocalJwtMinter,
    Session,
    SessionState,
    StaticTokenProvider,
)


def _default_config_path() -> Path:
    return Path(agent_evals.__file__).parent / "config" / "targets.yaml"


def _load_config(path: str | None) -> dict:
    load_dotenv(override=True)  # .env is authoritative (matches the hr-agent); supplies ${VAR} in the config
    p = Path(path) if path else _default_config_path()
    return expand_env(yaml.safe_load(p.read_text()) or {})


def _build_transport(target: dict, persist_dir: str | None) -> AgUiSseTransport:
    transport = target.get("transport", "agui_sse")
    if transport != "agui_sse":
        raise SystemExit(f"unsupported transport: {transport!r} (only agui_sse in v1)")
    tls = target.get("tls", {}) or {}
    if tls.get("use_truststore"):
        try:
            import truststore

            truststore.inject_into_ssl()
        except Exception as exc:  # missing extra
            raise SystemExit("tls.use_truststore set but truststore is unavailable; "
                             "run `pip install truststore`") from exc
    if tls.get("insecure"):
        verify: bool | str = False
    elif tls.get("ca_bundle"):
        verify = tls["ca_bundle"]
    else:
        verify = True
    return AgUiSseTransport(
        target["base_url"],
        persist_dir=persist_dir,
        verify=verify,
        create_thread=target.get("create_thread", True),
        graphql_url=target.get("graphql_url"),
    )


def _build_identity(target: dict) -> Identity:
    auth = target.get("auth", {}) or {}
    atype = auth.get("type", "local_jwt")
    if atype == "local_jwt":
        gpn = (auth.get("gpn") or "").strip()
        if not gpn:
            raise SystemExit("local_jwt target needs a GPN — set AGENT_EVALS_GPN in a .env "
                             "file (copy .env.example), or set auth.gpn in the target config.")
        scopes = auth.get("scopes")
        if scopes is None and auth.get("scope"):
            scopes = [auth["scope"]]
        return Identity(
            user_id=gpn,
            token_provider=LocalJwtMinter(
                gpn,
                user_claim=auth.get("user_claim", "ubs_auth_gpn"),
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

    runner = Runner(session_factory=session_factory, scorers=scorers, sink=sink,
                    judge=default_judge, config=scoring_cfg)
    run_name = args.run_name or f"{args.suite}-{args.target}-{time.strftime('%Y%m%d-%H%M%S')}"
    params = {
        "suite": args.suite, "target": args.target, "metrics": args.metrics,
        "judge": default_name, "sink": args.sink, "version": agent_evals.__version__,
    }
    print(f"Running {len(cases)} cases [{args.suite} → {args.target}] "
          f"with {len(scorers)} scorers, judge={default_name}, sink={args.sink} ...")
    report = runner.run(cases, run_name=run_name, params=params)
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
    r.add_argument("--limit", type=int, default=None, help="only run the first N cases")
    r.set_defaults(func=cmd_run)

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
    load_dotenv(override=True)  # make .env values (judge creds, etc.) authoritative for all commands
    args = build_parser().parse_args(argv)
    return args.func(args)
