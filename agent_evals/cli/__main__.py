"""CLI entry point: ``python -m agent_evals``.

Ported from chat-evals' ``evals/run.py`` (AICE-specific code dropped). Resolves
the project plug-in by name or path, picks the named target from the project's
``targets.yaml``, instantiates the right auth provider, and runs the eval via
:class:`~agent_evals.runners.mlflow_runner.MLflowRunner`.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from agent_evals.auth import AuthProvider, BearerAuth, EntraIdAuth, NoAuth
from agent_evals.core.project import (
    Project,
    list_projects,
    load_project,
    load_project_from_path,
)
from agent_evals.protocols.a2a.adapter import A2AAdapter
from agent_evals.runners.mlflow_runner import MLflowRunner

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger("agent_evals.cli")


# ----------------------------------------------------------------------------
# Env loading
# ----------------------------------------------------------------------------


def _load_env_walking_up() -> None:
    """Look for a ``.env`` walking up from cwd → repo root."""
    cur = Path.cwd()
    for _ in range(6):
        env_path = cur / ".env"
        if env_path.is_file():
            load_dotenv(env_path)
            logger.info("Loaded env file: %s", env_path)
            return
        if cur.parent == cur:
            break
        cur = cur.parent


# ----------------------------------------------------------------------------
# Auth resolution
# ----------------------------------------------------------------------------


def _resolve_auth(
    target_conf: dict[str, Any],
    *,
    explicit_token: str | None,
    auth_profile: str | None,
) -> AuthProvider:
    """Decide which AuthProvider to use for a given target.

    Resolution order:
    1. ``--token <raw-bearer-token>`` (CLI), if provided.
    2. ``--auth-profile entra-<env>``, which triggers ``EntraIdAuth`` using
       env-var-supplied Entra config.
    3. ``target.auth: oauth2-entra`` directive in ``targets.yaml`` → EntraIdAuth.
    4. ``target.requires_token: true`` with no token → error.
    5. Otherwise ``NoAuth``.
    """
    if explicit_token:
        return BearerAuth(explicit_token)

    auth_directive = (target_conf or {}).get("auth", "")
    profile = auth_profile or ""

    if profile.startswith("entra") or auth_directive == "oauth2-entra":
        return EntraIdAuth()

    if (target_conf or {}).get("requires_token"):
        raise SystemExit(
            "Target requires a token. Pass --token <raw-bearer-token> or set "
            "--auth-profile entra-<env> with the matching env vars."
        )

    return NoAuth()


# ----------------------------------------------------------------------------
# Run
# ----------------------------------------------------------------------------


def _build_adapter(target_conf: dict[str, Any], auth: AuthProvider, override_url: str | None) -> A2AAdapter:
    base_url = override_url or target_conf["url"]
    server_side = bool(target_conf.get("requires_token") or target_conf.get("server_side_threads"))
    return A2AAdapter(base_url=base_url, auth=auth, server_side_threads=server_side)


def _select_scorers(project: Project, mode: str, judge_model: str | None) -> list:
    if mode == "builtin":
        return list(project.builtin_scorers(model=judge_model))
    if mode == "custom":
        return list(project.custom_scorers())
    return [*project.builtin_scorers(model=judge_model), *project.custom_scorers()]


def run(args: argparse.Namespace) -> int:
    _load_env_walking_up()

    # ---- Resolve project ----
    if args.project_path:
        project = load_project_from_path(args.project_path)
    else:
        if not args.project:
            logger.error("--project or --project-path required. Installed: %s", list_projects())
            return 2
        project = load_project(args.project)

    # ---- Resolve target ----
    targets = project.targets()
    if args.target not in targets and not args.base_url:
        logger.error(
            "Unknown target '%s' for project '%s'. Available: %s",
            args.target, project.name, list(targets.keys()),
        )
        return 2
    target_conf = targets.get(args.target, {}) if args.target in targets else {}
    base_url = args.base_url or target_conf.get("url")
    if not base_url:
        logger.error("No base URL resolved. Pass --target or --base-url.")
        return 2

    logger.info("Project: %s | Target: %s | URL: %s", project.name, args.target, base_url)

    # ---- Build auth + adapter ----
    auth = _resolve_auth(target_conf, explicit_token=args.token, auth_profile=args.auth_profile)
    adapter = _build_adapter(target_conf, auth, override_url=args.base_url)

    # ---- Select scorers ----
    judge_model: str | None = None
    if args.scorers != "custom":
        deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-4o")
        judge_model = f"azure:/{deployment}"
    scorers = _select_scorers(project, args.scorers, judge_model)
    logger.info("Using %d scorers (mode=%s)", len(scorers), args.scorers)

    # ---- Hyperparameters ----
    hp: dict | None = None
    if args.hyperparameters:
        try:
            hp = json.loads(args.hyperparameters)
        except json.JSONDecodeError as exc:
            logger.error("Invalid --hyperparameters JSON: %s", exc)
            return 2

    # ---- Run ----
    datasets_to_run = (
        [args.dataset] if args.dataset else list(project.datasets().keys())
    )
    experiment_name = args.experiment_name or f"{project.name}-{args.target}"

    all_results: dict[str, Any] = {}
    for ds_name in datasets_to_run:
        logger.info("=" * 60)
        logger.info("Evaluating dataset: %s", ds_name)
        logger.info("=" * 60)
        dataset = project.get_dataset(ds_name)
        runner = MLflowRunner(
            experiment_name=f"{experiment_name}-{ds_name}",
            n_trials=args.n_trials,
            dataset=dataset,
            scorers=scorers,
            protocol_adapter=adapter,
            hyperparameters=hp,
        )
        all_results[ds_name] = runner.run()

    _print_summary(all_results)
    return 0


def _print_summary(results: dict[str, Any]) -> None:
    print("\n" + "=" * 70)
    print("AGENT-EVALS RESULTS SUMMARY")
    print("=" * 70)
    for ds_name, ds_results in results.items():
        print(f"\n--- dataset: {ds_name} ---")
        if isinstance(ds_results, dict):
            for run_name, metrics in ds_results.items():
                print(f"  run: {run_name}")
                if isinstance(metrics, dict):
                    for k, v in sorted(metrics.items()):
                        if isinstance(v, float):
                            print(f"    {k}: {v:.3f}")
                        else:
                            print(f"    {k}: {v}")
    print("\n" + "=" * 70)
    print("View detailed results: `mlflow ui`")
    print("=" * 70)


# ----------------------------------------------------------------------------
# Argparse
# ----------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agent-evals",
        description="Run MLflow-driven evals against a chat agent over A2A.",
    )
    p.add_argument("--project", default=None, help="Project plug-in name (entry-point)")
    p.add_argument("--project-path", default=None, help="Path to a project package for local dev")
    p.add_argument("--target", default="dev", help="Target name from the project's targets.yaml")
    p.add_argument("--base-url", default=None, help="Override target URL (ignores --target URL)")
    p.add_argument("--token", default=None, help="Static Bearer token (overrides --auth-profile)")
    p.add_argument("--auth-profile", default=None, help="Auth profile (e.g. entra-dev, entra-prod)")
    p.add_argument("--dataset", default=None, help="Dataset name (default: all datasets in project)")
    p.add_argument("--scorers", choices=["all", "builtin", "custom"], default="all")
    p.add_argument("--n-trials", type=int, default=1)
    p.add_argument("--experiment-name", default=None, help="MLflow experiment name override")
    p.add_argument("--hyperparameters", default=None, help='JSON grid: \'{"k": [v1, v2]}\'')
    p.add_argument("--list-projects", action="store_true", help="List installed project plug-ins and exit")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.list_projects:
        names = list_projects()
        if not names:
            print("(no project plug-ins installed)")
        else:
            for n in names:
                print(n)
        return 0

    try:
        return run(args)
    except Exception:
        logger.exception("Eval failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
