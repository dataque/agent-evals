"""backend project plug-in — production HR agent via A2A."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from agent_evals.core.project import Project
from agent_evals.scorers import get_builtin_scorers

from .datasets import ALL_DATASETS
from .scorers import backend_guidelines


class BackendProject(Project):
    name = "backend"

    def datasets(self):
        return ALL_DATASETS

    def builtin_scorers(self, model: str | None = None):
        return [*get_builtin_scorers(model=model), *backend_guidelines(model=model)]

    def targets(self) -> dict[str, dict[str, Any]]:
        targets_path = Path(__file__).resolve().parent.parent / "targets.yaml"
        with targets_path.open() as f:
            return yaml.safe_load(f) or {}

    def tool_schemas(self) -> dict[str, dict[str, Any]]:
        """Reserved for Phase 2 — Tool Result Schema Adherence scorer (CR#13)."""
        return {}


project = BackendProject()
