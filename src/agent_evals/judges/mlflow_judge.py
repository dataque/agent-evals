"""MLflow-native judge adapter (behind the neutral Judge interface).

Wraps ``mlflow.metrics.genai.make_genai_metric_from_prompt`` so a metric can be
judged by MLflow's LLM-judge machinery (model selected by a URI such as
``openai:/gpt-4o`` or ``endpoints:/azure-openai``). Requires the ``mlflow`` extra.

MLflow's prompt-judge API has shifted across versions; this adapter is wrapped
defensively and returns an error verdict (never raises) if the installed API
differs, so teams can finish wiring it for their version without breaking runs.
"""

from __future__ import annotations

import os

from ..core.judge import JudgeVerdict
from .base_openai import _SYSTEM


class MlflowJudge:
    name = "mlflow"

    def __init__(self, *, model: str | None = None, temperature: float = 0.0) -> None:
        self.model = model or os.getenv("MLFLOW_JUDGE_MODEL", "openai:/gpt-4o")
        self.temperature = temperature

    def evaluate(self, *, criteria, response, question=None, context=None, reference=None) -> JudgeVerdict:
        try:
            import pandas as pd
            from mlflow.metrics.genai import make_genai_metric_from_prompt

            judge_prompt = (
                f"{_SYSTEM}\n\n"
                f"CRITERIA:\n{criteria}\n\n"
                f"QUESTION:\n{question or '-'}\n\n"
                f"CONTEXT:\n{context or '-'}\n\n"
                f"REFERENCE:\n{reference or '-'}\n\n"
                "RESPONSE:\n{response}\n"
            )
            metric = make_genai_metric_from_prompt(
                name="agentevals_judge",
                judge_prompt=judge_prompt,
                model=self.model,
                greater_is_better=True,
                parameters={"temperature": self.temperature},
            )
            mv = metric.eval_fn(pd.Series([response]))
            score = float(mv.scores[0])
            # Normalize a 1-5 Likert score to 0..1 if the judge used that scale.
            if score > 1.0:
                score = (score - 1.0) / 4.0 if score <= 5.0 else min(score / 5.0, 1.0)
            score = max(0.0, min(1.0, score))
            justifications = getattr(mv, "justifications", None)
            rationale = justifications[0] if justifications else ""
            return JudgeVerdict(score=score, passed=score >= 0.5, rationale=rationale or "")
        except Exception as exc:
            return JudgeVerdict(score=0.0, passed=None, rationale=f"mlflow judge error: {exc}",
                                raw={"error": str(exc)})
