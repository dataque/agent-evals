#!/usr/bin/env python3
"""
A2A benchmark runner for an agent endpoint.

Supports two modes:
  - "local" (default): Uses LocalBenchmarker (local MLflow dev eval)
  - "aice":            Uses AICEBenchmarker (optional external benchmarker)

Supports multiple A2A endpoint targets defined in targets.yaml:
  - "direct" (default): a direct A2A endpoint
  - "remote-dev":       a remote service that requires --token

Usage:
    # Run against the direct target with the local benchmarker
    python -m evals --target direct --mode local --agent example

    # Run against a token-protected remote service
    python -m evals --target remote-dev --mode local --agent example --token <token>

    # Run with the optional AICEBenchmarker
    python -m evals --target direct --mode aice --agent example

    # Run all registered datasets
    python -m evals --target direct --mode local

    # Run with multiple trials
    python -m evals --target direct --mode local --agent example --n-trials 3

    # Override the target URL directly
    python -m evals --base-url "https://..." --mode local --agent example

Requirements:
    - For "local" mode: mlflow[databricks]>=3.10.0, plus judge credentials in
      .env (OPENAI_API_KEY, or AZURE_OPENAI_* for Azure).
    - For "aice" mode:  aice-benchmarker, mlflow[databricks]>=3.10.0

Design notes:
    - contextId / thread management only applies to "local" mode. AICE mode uses
      get_a2a_pred_fn from the aice package whose signature we don't control.
    - contextId is managed in the benchmarker loop, not inside predict_fn, so a
      single-turn sample gets a fresh contextId while a multi-turn item shares
      one contextId across its turns.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

import nest_asyncio

nest_asyncio.apply()

from .datasets import get_dataset, ALL_DATASETS
from .scorers import get_all_scorers, get_builtin_scorers, get_custom_scorers

import yaml

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger("benchmark.run")


# ---------------------------------------------------------------------------
# Load named A2A endpoint targets from targets.yaml
# ---------------------------------------------------------------------------
_TARGETS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "targets.yaml")
with open(_TARGETS_FILE, "r") as _f:
    TARGETS: dict[str, dict] = yaml.safe_load(_f)


def _resolve_judge_model() -> str | None:
    """Resolve the MLflow judge-model URI for local mode.

    Precedence: explicit JUDGE_MODEL env var → OpenAI (if OPENAI_API_KEY) →
    Azure (if AZURE_OPENAI_API_KEY) → None (MLflow default).
    """
    explicit = os.environ.get("JUDGE_MODEL")
    if explicit:
        return explicit
    if os.environ.get("OPENAI_API_KEY"):
        return "openai:/gpt-4o"
    if os.environ.get("AZURE_OPENAI_API_KEY"):
        deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-4o")
        return f"azure:/{deployment}"
    return None


def _flatten_multi_turn(dataset: list[dict]) -> list[dict]:
    """Flatten multi-turn items into independent single-turn items.

    Used for AICE mode which doesn't support multi-turn yet.
    Each turn becomes its own item with no shared context.
    """
    flat = []
    for item in dataset:
        if "turns" in item.get("inputs", {}):
            for turn in item["inputs"]["turns"]:
                flat.append({
                    "inputs": {"question": turn["question"]},
                    "expectations": turn.get("expectations", {}),
                })
        else:
            flat.append(item)
    return flat


def run_benchmark(
    base_url: str,
    agent_name: str | None = None,
    n_trials: int = 1,
    scorer_mode: str = "all",
    experiment_name: str = "a2a-agent-benchmark",
    hyperparameters: dict | None = None,
    mode: str = "local",
    headers: dict | None = None,
    requires_token: bool = False,
) -> dict:
    """Run the A2A benchmark evaluation.

    Parameters
    ----------
    base_url : str
        The A2A endpoint URL for the agent under test.
    agent_name : str or None
        Which dataset to evaluate. None = run all datasets sequentially.
    n_trials : int
        Number of evaluation trials per sample (for variance measurement).
    scorer_mode : str
        "all", "builtin", or "custom".
    experiment_name : str
        MLflow experiment name for tracking.
    hyperparameters : dict or None
        Optional hyperparameter grid for combinatorial evaluation.
    mode : str
        "local" to use LocalBenchmarker, "aice" to use AICEBenchmarker.
    headers : dict or None
        Optional HTTP headers (e.g. Authorization) for the A2A endpoint.
    requires_token : bool
        Whether this target requires token auth.

    Returns
    -------
    dict
        Mapping of agent_name -> benchmark result.
    """
    # For local mode, configure scorers to use the resolved judge model
    judge_model = _resolve_judge_model() if mode == "local" else None

    # Select scorers
    if scorer_mode == "builtin":
        scorers = get_builtin_scorers(model=judge_model)
    elif scorer_mode == "custom":
        scorers = get_custom_scorers()
    else:
        scorers = get_all_scorers(model=judge_model)

    logger.info("Using %d scorers (mode=%s)", len(scorers), scorer_mode)

    # Determine which datasets to evaluate
    if agent_name:
        agents_to_eval = [agent_name]
    else:
        agents_to_eval = list(ALL_DATASETS.keys())

    # Create predict function and benchmarker class based on mode
    if mode == "local":
        import uuid
        from .benchmarker.a2a_client import make_a2a_predict_fn, create_graphql_thread
        from .benchmarker.benchmarker import LocalBenchmarker as Benchmarker

        predict_fn = make_a2a_predict_fn(base_url=base_url, headers=headers)

        # Build thread_factory: token-protected targets mint a thread via
        # GraphQL; others use a random UUID as the contextId.
        if requires_token and headers:
            thread_factory = lambda: create_graphql_thread(base_url, headers)
        else:
            thread_factory = lambda: str(uuid.uuid4())
    else:
        from aice.benchmarker import AICEBenchmarker as Benchmarker
        from aice.benchmarker.cli.utils.a2a_pred_fn import get_a2a_pred_fn

        predict_fn = get_a2a_pred_fn(base_url=base_url)
        thread_factory = None  # AICE manages its own threads

    results = {}
    for agent in agents_to_eval:
        logger.info("=" * 60)
        logger.info("Evaluating dataset: %s", agent)
        logger.info("=" * 60)

        dataset = get_dataset(agent)

        # AICE doesn't support multi-turn — flatten to single-turn items
        if mode != "local":
            dataset = _flatten_multi_turn(dataset)

        start = time.monotonic()

        benchmarker_kwargs = {
            "experiment_name": f"{experiment_name}-{agent}",
            "n_trials": n_trials,
            "eval_dataset": dataset,
            "scorers": scorers,
            "predict_fn": predict_fn,
            "hyperparameters": hyperparameters,
        }
        if thread_factory is not None:
            benchmarker_kwargs["thread_factory"] = thread_factory

        controller = Benchmarker(**benchmarker_kwargs)

        result = controller.run_experiment()
        results[agent] = result

        elapsed = time.monotonic() - start
        logger.info("Dataset %s completed in %.1fs", agent, elapsed)

    return results


def print_summary(results: dict) -> None:
    """Print a formatted summary of benchmark results."""
    print("\n" + "=" * 70)
    print("BENCHMARK RESULTS SUMMARY")
    print("=" * 70)

    for run_name, eval_result in results.items():
        print(f"\n--- {run_name} ---")
        if isinstance(eval_result, dict):
            for key, value in sorted(eval_result.items()):
                if isinstance(value, float):
                    print(f"  {key}: {value:.3f}")
                else:
                    print(f"  {key}: {value}")
        elif hasattr(eval_result, "metrics") and eval_result.metrics:
            for metric, value in sorted(eval_result.metrics.items()):
                if isinstance(value, float):
                    print(f"  {metric}: {value:.3f}")
                else:
                    print(f"  {metric}: {value}")
        else:
            print(f"  Result: {eval_result}")

    print("\n" + "=" * 70)
    print("View detailed results in MLflow UI: mlflow ui")
    print("=" * 70)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run A2A benchmark evaluation for an agent endpoint"
    )
    parser.add_argument(
        "--target",
        choices=list(TARGETS.keys()),
        default="direct",
        help=f"A2A endpoint target (default: direct). Available: {', '.join(TARGETS.keys())}",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="Bearer token for token-protected targets",
    )
    parser.add_argument(
        "--mode",
        choices=["aice", "local"],
        default="local",
        help="Benchmarker to use: 'local' (LocalBenchmarker, default) or 'aice' (AICEBenchmarker)",
    )
    parser.add_argument(
        "--agent",
        choices=list(ALL_DATASETS.keys()),
        default=None,
        help="Dataset to evaluate (default: all datasets)",
    )
    parser.add_argument(
        "--n-trials",
        type=int,
        default=1,
        help="Number of evaluation trials per sample (default: 1)",
    )
    parser.add_argument(
        "--scorers",
        choices=["all", "builtin", "custom"],
        default="all",
        help="Which scorers to use (default: all)",
    )
    parser.add_argument(
        "--experiment-name",
        default=None,
        help="MLflow experiment name (default: a2a-agent-benchmark-{target})",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="Override the target URL (ignores --target)",
    )
    parser.add_argument(
        "--hyperparameters",
        default=None,
        help=(
            "JSON string of hyperparameter grid for combinatorial eval, "
            'e.g. \'{"model": ["gpt-4o", "gpt-4o-mini"], "temperature": [0.3, 0.7]}\''
        ),
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Load .env from project root (for judge credentials in local mode)
    from dotenv import load_dotenv
    _dir = os.path.dirname(os.path.abspath(__file__))
    while _dir != os.path.dirname(_dir):
        env_path = os.path.join(_dir, ".env")
        if os.path.isfile(env_path):
            load_dotenv(env_path)
            logger.info("Loaded env file: %s", env_path)
            break
        _dir = os.path.dirname(_dir)

    # Resolve target endpoint
    target = TARGETS[args.target]
    base_url = args.base_url or target["url"]
    requires_token = target["requires_token"] if not args.base_url else False

    logger.info("Target: %s (%s)", args.target, target["description"])
    logger.info("URL: %s", base_url)

    # Build auth headers if needed
    headers = None
    if requires_token:
        if not args.token:
            logger.error(
                "Target '%s' requires a Bearer token. Pass --token <token>.",
                args.target,
            )
            return 1
        headers = {"Authorization": f"Bearer {args.token}"}

    # Default experiment name includes target
    experiment_name = args.experiment_name or f"a2a-agent-benchmark-{args.target}"

    # Parse hyperparameters JSON if provided
    hyperparameters = None
    if args.hyperparameters:
        try:
            hyperparameters = json.loads(args.hyperparameters)
        except json.JSONDecodeError as exc:
            logger.error("Invalid --hyperparameters JSON: %s", exc)
            return 1

    try:
        results = run_benchmark(
            base_url=base_url,
            agent_name=args.agent,
            n_trials=args.n_trials,
            scorer_mode=args.scorers,
            experiment_name=experiment_name,
            hyperparameters=hyperparameters,
            mode=args.mode,
            headers=headers,
            requires_token=requires_token,
        )
        print_summary(results)
        return 0
    except Exception:
        logger.exception("Benchmark failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
