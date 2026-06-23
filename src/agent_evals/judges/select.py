"""Build judges by name and bind per-metric judge backends (for A/B).

The neutral ``Judge`` interface lets each judged metric pick its backend
independently — e.g. faithfulness via DeepEval, safety via MLflow, the rest via
Azure OpenAI — all configured rather than hard-coded.
"""

from __future__ import annotations

from collections.abc import Iterable

from ..core.judge import Judge

_ALIASES = {
    "langchain": "langchain_azure",
    "azure_langchain": "langchain_azure",
    "azure": "azure_openai",
    "azureopenai": "azure_openai",
    "azure_openai_sdk": "azure_openai",
    "stub": "heuristic",
    "none": "heuristic",
}


def build_judge(name: str | None, **kwargs) -> Judge:
    key = _ALIASES.get((name or "heuristic").lower(), (name or "heuristic").lower())
    if key == "langchain_azure":
        from .langchain_azure import LangchainAzureJudge

        return LangchainAzureJudge(**kwargs)
    if key == "azure_openai":
        from .base_openai import AzureOpenAIJudge

        return AzureOpenAIJudge(**kwargs)
    if key == "openai":
        from .base_openai import OpenAIJudge

        return OpenAIJudge(**kwargs)
    if key == "mlflow":
        from .mlflow_judge import MlflowJudge

        return MlflowJudge(**kwargs)
    if key == "deepeval":
        from .deepeval_judge import DeepEvalJudge

        return DeepEvalJudge(**kwargs)
    if key == "heuristic":
        from .heuristic import HeuristicJudge

        return HeuristicJudge(**kwargs)
    raise ValueError(f"unknown judge backend: {name!r}")


def apply_per_metric_judges(
    scorers: Iterable,
    *,
    default: Judge | str | None = None,
    per_metric: dict[str, str] | None = None,
) -> list:
    """Set ``scorer.judge`` for judged scorers from a per-metric backend map,
    falling back to ``default``. Backends are built once and reused."""
    per_metric = per_metric or {}
    cache: dict[str, Judge] = {}

    def get(name: str) -> Judge:
        if name not in cache:
            cache[name] = build_judge(name)
        return cache[name]

    default_judge = default if isinstance(default, Judge) else (build_judge(default) if default else None)

    result = list(scorers)
    for s in result:
        if not getattr(s.spec, "needs_judge", False):
            continue
        backend = per_metric.get(s.spec.metric)
        if backend:
            s.judge = get(backend)
        elif getattr(s, "judge", None) is None and default_judge is not None:
            s.judge = default_judge
    return result
