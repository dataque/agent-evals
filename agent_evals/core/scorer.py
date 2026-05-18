"""Scorer protocol.

The framework's scorers are simple callables compatible with MLflow's
``@mlflow.genai.scorers.scorer`` decorator. We don't reinvent the wheel —
``Scorer`` is a typing protocol that matches MLflow's signature so any scorer
can be plugged into either MLflow's ``evaluate`` or a future non-MLflow runner.

Concrete built-in / custom scorers live in ``agent_evals.scorers``.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Scorer(Protocol):
    """A scorer callable. MLflow's ``@scorer`` decorator produces objects
    matching this shape."""

    def __call__(self, **kwargs: Any) -> Any:
        ...

    # MLflow's @scorer decorator attaches metadata fields used by the runner.
    # They are optional for non-MLflow scorers.
    name: str | None  # type: ignore[assignment]


# Re-export MLflow's @scorer decorator when available so projects can author
# scorers without importing MLflow directly.
try:
    from mlflow.genai.scorers import scorer  # noqa: F401
except ImportError:  # pragma: no cover

    def scorer(fn):  # type: ignore[no-redef]
        """Stub for environments without mlflow installed (testing only)."""
        return fn


__all__ = ["Scorer", "scorer"]
