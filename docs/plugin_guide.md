# Authoring a project plug-in

A project plug-in is a self-contained eval definition: datasets, project-specific scorer rubrics, target endpoints, and (optionally) JSON Schemas for tool result validation. Plug-ins are normal Python packages discovered via entry points.

## Minimum viable plug-in

```
projects/my_project/
├── pyproject.toml
├── targets.yaml
└── agent_evals_my_project/
    ├── __init__.py        # exports `project` (a Project instance)
    ├── datasets.py        # exports ALL_DATASETS: dict[str, list[dict]]
    └── scorers.py         # optional: project-specific Guidelines rubrics
```

### `pyproject.toml`

```toml
[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "agent-evals-my-project"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["agent-evals"]

[project.entry-points."agent_evals.projects"]
my_project = "agent_evals_my_project:project"

[tool.setuptools.packages.find]
include = ["agent_evals_my_project*"]
```

### `agent_evals_my_project/__init__.py`

```python
from pathlib import Path
import yaml
from agent_evals.core.project import Project
from agent_evals.scorers import get_builtin_scorers

from .datasets import ALL_DATASETS

class MyProject(Project):
    name = "my_project"

    def datasets(self):
        return ALL_DATASETS

    def builtin_scorers(self, model=None):
        return list(get_builtin_scorers(model=model))

    def targets(self):
        return yaml.safe_load((Path(__file__).resolve().parent.parent / "targets.yaml").read_text())

project = MyProject()
```

### `targets.yaml`

```yaml
dev:
  url: "https://your-dev-endpoint/api/v1/bff/ai/agent/a2a"
  description: "Dev environment"
  requires_token: true
  auth: oauth2-entra        # or omit for static-token / no auth
```

### `datasets.py`

```python
ALL_DATASETS = {
    "main": [
        {
            "inputs": {"question": "What can you help me with?"},
            "expectations": {
                "response_must_contain": ["help"],
                "expected_tool_calls": [],
            },
        },
        # ...
    ],
}
```

## Installing and running

```bash
pip install -e .
pip install -e projects/my_project
python -m agent_evals --project my_project --target dev --scorers all
```

## Adding project-specific Guidelines

```python
# agent_evals_my_project/scorers.py
from agent_evals.scorers import build_guidelines_scorer

def my_guidelines(model=None):
    return [
        build_guidelines_scorer(
            name="my_topic",
            guidelines="The response must stay within X domain ...",
            model=model,
        ),
    ]
```

Then override in your `Project`:

```python
def builtin_scorers(self, model=None):
    return [*get_builtin_scorers(model=model), *my_guidelines(model=model)]
```

## Expectation keys recognised by built-in scorers

| Key | Type | Used by |
|---|---|---|
| `expected_response` | `str` | `Correctness` |
| `response_must_contain` | `list[str]` | `response_completeness` |
| `expected_tool_calls` | `list[str]` | `tool_trace_f1` |
| `expected_tool_args` | `dict[tool, dict]` | `tool_argument_correctness` |
| `max_steps` | `int` | `step_efficiency` |
| `expected_routes` | `list[str]` | `plan_quality` |
| `allowed_tool_calls` | `list[str]` | `plan_quality` |
| `expected_actions` | `list[str]` | `audit_log_action_taken` |
| `expected_artifacts` | `dict[name, schema_id]` | `card_format_correctness` |

A scorer returning `None` means the row had no expectation for that metric — the row is skipped for that scorer, so datasets can mix expectations per item without breaking aggregation.
