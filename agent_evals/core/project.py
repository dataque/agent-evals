"""Project plug-in interface.

A ``Project`` is a self-contained eval definition: datasets, scorers, target
endpoints, and (optionally) JSON Schemas for tool result validation. Projects
are loaded by name via Python entry points or by path via the CLI's
``--project-path`` flag.

Discovery via entry points:

    [project.entry-points."agent_evals.projects"]
    hr_agent_poc = "agent_evals_hr_poc:project"

The exported attribute must be a ``Project`` instance.
"""

from __future__ import annotations

import importlib
import importlib.metadata as importlib_metadata
import importlib.util
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from .dataset import Dataset
from .scorer import Scorer


class Project(ABC):
    """A pluggable eval project."""

    name: str = ""

    @abstractmethod
    def datasets(self) -> dict[str, Dataset]:
        """Mapping of dataset name → dataset items."""

    def get_dataset(self, name: str | None = None) -> Dataset:
        """Return one dataset by name, or all datasets merged."""
        all_ds = self.datasets()
        if name is not None:
            if name not in all_ds:
                raise ValueError(
                    f"Unknown dataset '{name}' for project '{self.name}'. "
                    f"Available: {list(all_ds.keys())}"
                )
            return all_ds[name]
        merged: Dataset = []
        for ds_name, ds in all_ds.items():
            for item in ds:
                item_copy = {**item}
                item_copy.setdefault("metadata", {})
                item_copy["metadata"]["dataset"] = ds_name
                merged.append(item_copy)
        return merged

    def builtin_scorers(self, model: str | None = None) -> list[Scorer]:
        """Return the project's built-in MLflow scorers (judge-based).

        Default: framework's :func:`agent_evals.scorers.get_builtin_scorers`.
        Projects override to provide project-specific Guidelines rubrics.
        """
        from agent_evals.scorers import get_builtin_scorers

        return get_builtin_scorers(model=model)

    def custom_scorers(self) -> list[Scorer]:
        """Return the project's custom (text + trace-aware) scorers.

        Default: framework's standard custom-scorer preset.
        """
        from agent_evals.scorers import (
            audit_log_action_taken,
            card_format_correctness,
            plan_quality,
            response_completeness,
            step_efficiency,
            tool_argument_correctness,
            tool_trace_f1,
        )

        return [
            response_completeness,
            tool_trace_f1,
            tool_argument_correctness,
            step_efficiency,
            plan_quality,
            audit_log_action_taken,
            card_format_correctness,
        ]

    @abstractmethod
    def targets(self) -> dict[str, dict[str, Any]]:
        """Mapping of target name → target config (url, description,
        requires_token, auth profile reference)."""

    def tool_schemas(self) -> dict[str, dict[str, Any]]:
        """Mapping of tool_name → JSON Schema for that tool's result payload.

        Consumed by :mod:`agent_evals.scorers.schema_adherence` (Phase 2).
        Default: empty (no schema enforcement).
        """
        return {}


# ----------------------------------------------------------------------------
# Project discovery
# ----------------------------------------------------------------------------


def load_project(name: str) -> Project:
    """Resolve a project by entry-point name.

    Looks under the ``agent_evals.projects`` entry-point group.
    """
    eps = importlib_metadata.entry_points(group="agent_evals.projects")
    for ep in eps:
        if ep.name == name:
            obj = ep.load()
            if isinstance(obj, type) and issubclass(obj, Project):
                return obj()
            if isinstance(obj, Project):
                return obj
            raise TypeError(
                f"Entry point '{name}' loaded {obj!r}, expected Project instance or class."
            )
    raise ValueError(f"No project entry point named '{name}'. Installed: {[ep.name for ep in eps]}")


def load_project_from_path(path: str | Path) -> Project:
    """Load a project from a directory containing an ``agent_evals_*`` package.

    Useful for local development before the project is pip-installed.
    The directory must have a ``pyproject.toml`` advertising a project
    entry-point, or a top-level ``project.py`` exposing a ``project`` attribute.
    """
    path = Path(path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"Project path does not exist: {path}")

    # Walk the path for a package with an __init__.py exposing `project`.
    for sub in path.iterdir():
        if not sub.is_dir():
            continue
        init = sub / "__init__.py"
        if not init.exists():
            continue
        if sub.name.startswith("agent_evals_"):
            spec = importlib.util.spec_from_file_location(sub.name, init)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules[sub.name] = module
            spec.loader.exec_module(module)
            obj = getattr(module, "project", None)
            if isinstance(obj, Project):
                return obj
            if isinstance(obj, type) and issubclass(obj, Project):
                return obj()

    raise ValueError(
        f"Could not locate an agent_evals_* package with a `project` attribute under {path}"
    )


def list_projects() -> list[str]:
    """Names of installed project plug-ins."""
    eps = importlib_metadata.entry_points(group="agent_evals.projects")
    return sorted(ep.name for ep in eps)
