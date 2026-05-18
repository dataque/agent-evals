"""Built-in MLflow GenAI scorers.

The framework ships only project-agnostic built-ins: ``Correctness``,
``RelevanceToQuery``, ``Safety``, plus a ``build_guidelines_scorer`` factory
that projects use to add their own natural-language rubrics. The HR-specific
rubrics from chat-evals (``professional_tone``, ``hr_relevance``,
``data_privacy``) now live in the ``hr_agent_poc`` project plug-in — they are
project policy, not framework policy.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("agent_evals.scorers.builtin")

try:
    from mlflow.genai.scorers import (
        Correctness,
        Guidelines,
        RelevanceToQuery,
        Safety,
    )

    _MLFLOW_AVAILABLE = True
except ImportError:  # pragma: no cover
    _MLFLOW_AVAILABLE = False
    Correctness = RelevanceToQuery = Safety = Guidelines = None  # type: ignore[assignment]


def get_builtin_scorers(model: str | None = None) -> list:
    """Return the framework's project-agnostic built-in scorers.

    Parameters
    ----------
    model
        Judge model URI, e.g. ``"openai:/gpt-4o"`` or ``"endpoints:/azure-openai"``.
        ``None`` uses MLflow's default judge configuration.
    """
    if not _MLFLOW_AVAILABLE:
        raise ImportError(
            "mlflow is required for built-in scorers. Install via "
            "`pip install 'mlflow[databricks]>=3.10.0'`."
        )
    kwargs: dict = {}
    if model:
        kwargs["model"] = model

    return [
        Correctness(**kwargs),
        RelevanceToQuery(**kwargs),
        Safety(**kwargs),
    ]


def build_guidelines_scorer(
    name: str,
    guidelines: str,
    model: str | None = None,
):
    """Factory for one project-specific Guidelines rubric.

    Returns an MLflow ``Guidelines`` scorer instance. Use one call per rubric
    in a project's :py:meth:`agent_evals.core.project.Project.builtin_scorers`
    implementation.

    Example::

        Guidelines(name="professional_tone", guidelines="...corporate tone...")
    """
    if not _MLFLOW_AVAILABLE:
        raise ImportError("mlflow is required to construct Guidelines scorers.")
    kwargs: dict = {"name": name, "guidelines": guidelines}
    if model:
        kwargs["model"] = model
    return Guidelines(**kwargs)
