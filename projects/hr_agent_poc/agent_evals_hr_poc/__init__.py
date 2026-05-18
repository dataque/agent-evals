"""HR Agent PoC project plug-in — smoke test for the framework's A2A path."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from agent_evals.core.project import Project
from agent_evals.scorers import get_builtin_scorers

from .datasets import ALL_DATASETS
from .scorers import hr_poc_guidelines


class HrAgentPocProject(Project):
    name = "hr_agent_poc"

    def datasets(self):
        return ALL_DATASETS

    def builtin_scorers(self, model: str | None = None):
        return [*get_builtin_scorers(model=model), *hr_poc_guidelines(model=model)]

    def targets(self) -> dict[str, dict[str, Any]]:
        targets_path = Path(__file__).resolve().parent.parent / "targets.yaml"
        with targets_path.open() as f:
            return yaml.safe_load(f) or {}


project = HrAgentPocProject()
