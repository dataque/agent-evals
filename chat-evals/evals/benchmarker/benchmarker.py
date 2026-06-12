"""
LocalBenchmarker — local MLflow-based evaluation matching AICEBenchmarker's
interface.

Uses mlflow.genai.evaluate with local file-based tracking. Configures an LLM
judge (OpenAI or Azure OpenAI) for the built-in scorers.
"""

from __future__ import annotations

import itertools
import logging
import os
import time
import uuid
from typing import Any, Callable

import mlflow
import pandas as pd

from .a2a_client import A2ARequestError, A2AResponse

logger = logging.getLogger("benchmarker.benchmarker")


def _configure_judge_env():
    """Configure the environment so MLflow's built-in LLM-judge scorers can
    reach a judge model.

    Direct OpenAI works out of the box via ``OPENAI_API_KEY``. For Azure
    OpenAI, map the ``AZURE_OPENAI_*`` vars to the ``AZURE_*`` vars that
    litellm (used by MLflow) expects.
    """
    if os.environ.get("OPENAI_API_KEY"):
        return  # direct OpenAI judge is ready

    api_key = os.environ.get("AZURE_OPENAI_API_KEY", "")
    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
    api_version = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")

    if not api_key or not endpoint:
        logger.warning(
            "No judge credentials found. Set OPENAI_API_KEY (OpenAI) or "
            "AZURE_OPENAI_API_KEY + AZURE_OPENAI_ENDPOINT (Azure). "
            "LLM-judge scorers may fail."
        )
        return

    # litellm azure/ prefix env vars
    os.environ.setdefault("AZURE_API_KEY", api_key)
    os.environ.setdefault("AZURE_API_BASE", endpoint)
    os.environ.setdefault("AZURE_API_VERSION", api_version)

    logger.info("Configured Azure OpenAI judge: endpoint=%s", endpoint)


def _default_thread_factory() -> str:
    """Generate a random UUID as contextId."""
    return str(uuid.uuid4())


class LocalBenchmarker:
    """Local MLflow-based benchmarker with the same interface as AICEBenchmarker.

    Parameters
    ----------
    experiment_name : str
        MLflow experiment name for tracking.
    n_trials : int
        Number of times to evaluate each input from the dataset.
    eval_dataset : list[dict]
        Evaluation dataset — list of dicts with ``inputs`` and ``expectations``.
    scorers : list
        List of MLflow scorer objects (built-in and/or custom).
    predict_fn : Callable
        A function ``(question: str, context_id: str | None, **kwargs) -> str``
        that returns the agent's text response.
    hyperparameters : dict or None
        Optional hyperparameter grid for combinatorial evaluation.
        e.g. ``{"model": ["gpt-4o", "gpt-4o-mini"], "temperature": [0.3, 0.7]}``.
    thread_factory : Callable or None
        A callable that returns a new contextId string. Called once per
        single-turn sample and once per multi-turn conversation. Defaults to
        UUID generation.
    """

    def __init__(
        self,
        experiment_name: str,
        n_trials: int,
        eval_dataset: list[dict],
        scorers: list,
        predict_fn: Callable,
        hyperparameters: dict | None = None,
        thread_factory: Callable[[], str] | None = None,
    ):
        self.experiment_name = experiment_name
        self.n_trials = n_trials
        self.eval_dataset = eval_dataset
        self.scorers = scorers
        self.predict_fn = predict_fn
        self.hyperparameters = hyperparameters
        self.thread_factory = thread_factory or _default_thread_factory

    def _generate_hyperparam_combos(self) -> list[dict]:
        """Generate all combinations from the hyperparameter grid."""
        if not self.hyperparameters:
            return [{}]

        keys = list(self.hyperparameters.keys())
        values = [
            v if isinstance(v, list) else [v]
            for v in self.hyperparameters.values()
        ]

        combos = []
        for combo_values in itertools.product(*values):
            combos.append(dict(zip(keys, combo_values)))
        return combos

    def _row_from_prediction(
        self,
        *,
        inputs: dict,
        expectations: dict,
        prediction,
    ) -> dict:
        """Normalise a predict_fn return value into an MLflow eval row.

        Accepts either an ``A2AResponse`` (preferred — surfaces trace, artifacts,
        task metadata) or a bare ``str`` (legacy text-only contract). The ``outputs``
        column is always the final assistant text, so existing text-based scorers
        keep working; new trace/artifact/metadata columns are added when available.
        """
        if isinstance(prediction, A2AResponse):
            return {
                "inputs": inputs,
                "outputs": prediction.text,
                "expectations": expectations,
                "trace": prediction.trace,
                "artifacts": prediction.artifacts,
                "task_metadata": prediction.metadata,
                "state": prediction.state,
            }
        text = prediction if isinstance(prediction, str) else ""
        return {
            "inputs": inputs,
            "outputs": text,
            "expectations": expectations,
            "trace": {},
            "artifacts": {},
            "task_metadata": {},
            "state": "",
        }

    def _collect_predictions(self, hyperparam_combo: dict) -> list[dict]:
        """Run predict_fn on each dataset item and collect outputs.

        Handles both single-turn and multi-turn items:
        - Single-turn (inputs.question): fresh contextId, one call, one result row
        - Multi-turn (inputs.turns): shared contextId, sequential calls, one row per turn
        """
        results = []
        for item in self.eval_dataset:
            if "turns" in item["inputs"]:
                context_id = self.thread_factory()
                scenario = item["inputs"].get("scenario", "unnamed")
                turns = item["inputs"]["turns"]

                for i, turn in enumerate(turns, 1):
                    question = turn["question"]
                    try:
                        prediction = self.predict_fn(
                            question, context_id=context_id, **hyperparam_combo
                        )
                    except A2ARequestError as exc:
                        logger.error(
                            "Turn %d/%d failed (scenario=%s, context_id=%s): %s",
                            i, len(turns), scenario, context_id, exc,
                        )
                        prediction = ""

                    results.append(self._row_from_prediction(
                        inputs={"question": question},
                        expectations=turn.get("expectations", {}),
                        prediction=prediction,
                    ))
            else:
                context_id = self.thread_factory()
                question = item["inputs"]["question"]

                try:
                    prediction = self.predict_fn(
                        question, context_id=context_id, **hyperparam_combo
                    )
                except A2ARequestError as exc:
                    logger.error("Sample failed (context_id=%s): %s", context_id, exc)
                    prediction = ""

                results.append(self._row_from_prediction(
                    inputs=item["inputs"],
                    expectations=item.get("expectations", {}),
                    prediction=prediction,
                ))
        return results

    def _run_single_trial(
        self,
        trial: int,
        hyperparam_combo: dict,
    ) -> dict[str, Any]:
        """Run a single evaluation trial and return metrics."""
        logger.info(
            "Trial %d — hyperparameters: %s",
            trial,
            hyperparam_combo or "(default)",
        )

        predictions = self._collect_predictions(hyperparam_combo)
        df = pd.DataFrame(predictions)

        # mlflow.genai.evaluate auto-deserialises the `trace` column via
        # Trace.from_dict, which raises on empty dicts. Targets that don't emit
        # the v1 execution_trace artifact leave the column populated with {} for
        # those runs. Normalise empty values to None and drop the column if
        # nothing was captured; trace-aware scorers handle a missing trace by
        # returning None (skipped).
        if "trace" in df.columns:
            df["trace"] = df["trace"].apply(
                lambda t: t if isinstance(t, dict) and t else None
            )
            if df["trace"].isna().all():
                df = df.drop(columns=["trace"])

        eval_result = mlflow.genai.evaluate(
            data=df,
            scorers=self.scorers,
        )

        metrics = {}
        if hasattr(eval_result, "metrics") and eval_result.metrics:
            metrics = dict(eval_result.metrics)

        return metrics

    def run_experiment(self) -> dict[str, Any]:
        """Run the full benchmark experiment.

        Returns a dict of aggregated metrics across all trials and
        hyperparameter combinations.
        """
        _configure_judge_env()
        mlflow.set_experiment(self.experiment_name)

        combos = self._generate_hyperparam_combos()
        all_results: dict[str, Any] = {}

        total_start = time.monotonic()

        for combo in combos:
            combo_label = str(combo) if combo else "default"

            for trial in range(1, self.n_trials + 1):
                run_name = f"trial-{trial}"
                if combo:
                    combo_str = "_".join(f"{k}={v}" for k, v in combo.items())
                    run_name = f"{combo_str}_trial-{trial}"

                with mlflow.start_run(run_name=run_name):
                    mlflow.log_params({
                        "trial": trial,
                        "n_trials": self.n_trials,
                        "n_samples": len(self.eval_dataset),
                        **{f"hp_{k}": v for k, v in combo.items()},
                    })

                    metrics = self._run_single_trial(trial, combo)

                    for name, value in metrics.items():
                        logger.info("  %s: %s", name, value)

                    result_key = f"{combo_label}_trial_{trial}"
                    all_results[result_key] = metrics

        elapsed = time.monotonic() - total_start
        logger.info("Experiment completed in %.1fs", elapsed)

        return all_results
