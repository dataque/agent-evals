"""Generic MLflow runner.

Extracted from chat-evals' ``HRBenchmarker`` (`evals/hr_benchmarker/benchmarker.py`).
The shape is identical; the differences are:

- Accepts a :class:`~agent_evals.core.protocol.ProtocolAdapter` instead of a
  raw ``predict_fn`` + ``thread_factory`` pair. The adapter supplies both.
- The eval-row column set is the same as chat-evals' (``outputs``, ``trace``,
  ``artifacts``, ``task_metadata``, ``state``) so the ported scorers consume
  rows produced by either implementation interchangeably.
- Azure-OpenAI-judge env-var bridging is preserved (moved into this module).
"""

from __future__ import annotations

import itertools
import logging
import os
import time
from typing import Any, Iterable

import mlflow
import pandas as pd

from agent_evals.core.dataset import Dataset
from agent_evals.core.protocol import PredictRequest, PredictResponse, ProtocolAdapter
from agent_evals.core.scorer import Scorer
from agent_evals.core.trace import Trace

logger = logging.getLogger("agent_evals.runners.mlflow_runner")


def _configure_azure_openai_judge() -> None:
    """Map ``AZURE_OPENAI_*`` env vars to ``AZURE_*`` vars that litellm /
    MLflow's built-in judge scorers expect.

    Identical to chat-evals' ``_configure_azure_openai_judge``.
    """
    api_key = os.environ.get("AZURE_OPENAI_API_KEY", "")
    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
    deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-4o")
    api_version = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")

    if not api_key or not endpoint:
        logger.warning(
            "AZURE_OPENAI_API_KEY and AZURE_OPENAI_ENDPOINT must be set for "
            "LLM-judge scorers to work. Scorers may fail."
        )
        return

    os.environ.setdefault("AZURE_API_KEY", api_key)
    os.environ.setdefault("AZURE_API_BASE", endpoint)
    os.environ.setdefault("AZURE_API_VERSION", api_version)
    logger.info(
        "Configured Azure OpenAI judge: endpoint=%s, deployment=%s", endpoint, deployment
    )


class MLflowRunner:
    """Run an eval dataset through a protocol adapter, score with MLflow.

    Parameters
    ----------
    experiment_name
        MLflow experiment name.
    n_trials
        Number of evaluation trials per sample (variance measurement).
    dataset
        Eval dataset — list of ``{"inputs": ..., "expectations": ...}`` items.
    scorers
        List of MLflow scorers (built-in and/or custom).
    protocol_adapter
        The :class:`ProtocolAdapter` that knows how to talk to the agent.
    hyperparameters
        Optional grid for combinatorial eval, e.g.
        ``{"model": ["gpt-4o", "gpt-4o-mini"], "temperature": [0.3, 0.7]}``.
    """

    def __init__(
        self,
        experiment_name: str,
        n_trials: int,
        dataset: Dataset,
        scorers: Iterable[Scorer],
        protocol_adapter: ProtocolAdapter,
        hyperparameters: dict[str, list] | None = None,
    ):
        self.experiment_name = experiment_name
        self.n_trials = n_trials
        self.dataset = dataset
        self.scorers = list(scorers)
        self.adapter = protocol_adapter
        self.hyperparameters = hyperparameters

    # ------------------------------------------------------------------
    # Hyperparam expansion
    # ------------------------------------------------------------------

    def _hyperparam_combos(self) -> list[dict]:
        if not self.hyperparameters:
            return [{}]
        keys = list(self.hyperparameters.keys())
        values = [
            v if isinstance(v, list) else [v] for v in self.hyperparameters.values()
        ]
        return [dict(zip(keys, combo)) for combo in itertools.product(*values)]

    # ------------------------------------------------------------------
    # Row construction
    # ------------------------------------------------------------------

    @staticmethod
    def _row_from_prediction(
        *,
        inputs: dict,
        expectations: dict,
        prediction: Any,
    ) -> dict:
        """Normalise a predict_fn return into a flat eval row.

        Accepts a :class:`PredictResponse` (preferred) or a bare ``str`` (legacy).
        The ``trace`` column carries the raw dict so the trace-aware scorers
        (which accept both dict and Trace via ``_wrap``) keep working.
        """
        if isinstance(prediction, PredictResponse):
            trace = prediction.trace.raw if isinstance(prediction.trace, Trace) else (prediction.trace or {})
            return {
                "inputs": inputs,
                "outputs": prediction.text,
                "expectations": expectations,
                "trace": trace,
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

    # ------------------------------------------------------------------
    # Prediction loop
    # ------------------------------------------------------------------

    def _collect_predictions(self, hyperparam_combo: dict) -> list[dict]:
        """Run the adapter on each dataset item; emit one MLflow row per turn.

        Single-turn (``inputs.question``): fresh thread per sample, one call.
        Multi-turn (``inputs.turns``): shared thread across turns, one row per turn.
        """
        results: list[dict] = []
        for item in self.dataset:
            inputs = item.get("inputs", {})
            if "turns" in inputs:
                thread_id = self.adapter.new_thread_id()
                scenario = inputs.get("scenario", "unnamed")
                turns = inputs["turns"]

                for i, turn in enumerate(turns, 1):
                    question = turn["question"]
                    try:
                        prediction = self.adapter.send(
                            PredictRequest(question=question, thread_id=thread_id),
                            **hyperparam_combo,
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.error(
                            "Turn %d/%d failed (scenario=%s, thread=%s): %s",
                            i, len(turns), scenario, thread_id, exc,
                        )
                        prediction = ""

                    results.append(
                        self._row_from_prediction(
                            inputs={"question": question},
                            expectations=turn.get("expectations", {}),
                            prediction=prediction,
                        )
                    )
            else:
                thread_id = self.adapter.new_thread_id()
                question = inputs.get("question", "")
                try:
                    prediction = self.adapter.send(
                        PredictRequest(question=question, thread_id=thread_id),
                        **hyperparam_combo,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error("Sample failed (thread=%s): %s", thread_id, exc)
                    prediction = ""

                results.append(
                    self._row_from_prediction(
                        inputs=inputs,
                        expectations=item.get("expectations", {}),
                        prediction=prediction,
                    )
                )
        return results

    # ------------------------------------------------------------------
    # Trial + experiment loop
    # ------------------------------------------------------------------

    def _run_single_trial(self, trial: int, hyperparam_combo: dict) -> dict[str, Any]:
        logger.info(
            "Trial %d — hyperparameters: %s",
            trial,
            hyperparam_combo or "(default)",
        )

        predictions = self._collect_predictions(hyperparam_combo)
        df = pd.DataFrame(predictions)

        # mlflow.genai.evaluate auto-deserialises the ``trace`` column via
        # Trace.from_dict, which raises on empty dicts. When trace capture is
        # not yet wired in the backend (e.g. backend without the eval-tap CR),
        # the column is filled with ``{}``. Normalise empties to ``None`` and
        # drop the column if nothing was captured; trace-aware scorers return
        # ``None`` for missing trace and are skipped.
        if "trace" in df.columns:
            df["trace"] = df["trace"].apply(
                lambda t: t if isinstance(t, dict) and t else None
            )
            if df["trace"].isna().all():
                df = df.drop(columns=["trace"])

        eval_result = mlflow.genai.evaluate(data=df, scorers=self.scorers)

        metrics: dict[str, Any] = {}
        if hasattr(eval_result, "metrics") and eval_result.metrics:
            metrics = dict(eval_result.metrics)
        return metrics

    def run(self) -> dict[str, Any]:
        """Run the full experiment. Returns aggregated metrics by combo / trial."""
        _configure_azure_openai_judge()
        mlflow.set_experiment(self.experiment_name)

        combos = self._hyperparam_combos()
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
                    mlflow.log_params(
                        {
                            "trial": trial,
                            "n_trials": self.n_trials,
                            "n_samples": len(self.dataset),
                            **{f"hp_{k}": v for k, v in combo.items()},
                        }
                    )
                    metrics = self._run_single_trial(trial, combo)
                    for name, value in metrics.items():
                        logger.info("  %s: %s", name, value)
                    all_results[f"{combo_label}_trial_{trial}"] = metrics

        elapsed = time.monotonic() - total_start
        logger.info("Experiment completed in %.1fs", elapsed)
        return all_results
