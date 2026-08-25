"""Build judges by name and bind per-metric judge backends (for A/B).

The neutral ``Judge`` interface lets each judged metric pick its backend
independently — e.g. faithfulness via DeepEval, safety via MLflow, the rest via
Azure OpenAI — all configured rather than hard-coded.
"""

from __future__ import annotations

from collections.abc import Iterable

from ..core.judge import Judge

_ALIASES = {
    "azure": "azure_openai",
    "azureopenai": "azure_openai",
    "stub": "heuristic",
    "none": "heuristic",
}


def build_judge(name: str | None, **kwargs) -> Judge:
    key = _ALIASES.get((name or "heuristic").lower(), (name or "heuristic").lower())
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


# Judge settings worth recording in a run's params, read off the constructed
# judge rather than off the config, so what is reported is what actually scored.
_DESCRIBE_FIELDS = ("model", "temperature", "max_tokens", "api_version")


def describe_judge(judge: Judge | None) -> dict:
    """Identify a judge instance for ``params.json`` (E19).

    Unlike the system under test's model, the judge is configured BY the eval, so
    its identity is observed rather than operator-declared: backend name plus
    whichever of model/temperature/max_tokens/api_version the backend carries.
    A judge class may override this by defining its own ``describe()``.

    Deliberately never returns credentials: the deployment/model name is
    provenance, the API key is not.

    ``temperature``/``max_tokens`` are what the harness ASKS for. The LLM judge
    degrades its request shape on a model's first 400 (GPT-5 / o-series reject a
    non-default temperature), and params are written before the first call, so
    read those two as configured intent rather than as what every request carried.
    """
    if judge is None:
        return {}
    describe = getattr(judge, "describe", None)
    if callable(describe):
        return dict(describe())
    out = {"backend": getattr(judge, "name", type(judge).__name__)}
    for field in _DESCRIBE_FIELDS:
        value = getattr(judge, field, None)
        if value not in (None, ""):
            out[field] = value
    out["source"] = "observed"
    return out
