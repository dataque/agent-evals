"""Load eval suites (YAML/JSON) into ``EvalCase`` objects.

A suite is a list of raw cases (the chat-evals dict shape), or a mapping with a
``cases:`` key. ``name_or_path`` resolves to an on-disk file/dir, or a bundled
suite directory under ``agent_evals/datasets/``.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ..core.case import EvalCase

_BUNDLED = Path(__file__).parent


def _iter_paths(name_or_path: str) -> tuple[list[Path], str]:
    p = Path(name_or_path)
    if p.exists():
        if p.is_dir():
            return sorted(p.glob("*.y*ml")) + sorted(p.glob("*.json")), p.name
        return [p], p.stem
    bundled = _BUNDLED / name_or_path
    if bundled.is_dir():
        return sorted(bundled.glob("*.y*ml")) + sorted(bundled.glob("*.json")), name_or_path
    raise FileNotFoundError(f"eval suite not found: {name_or_path!r}")


def load_suite(name_or_path: str) -> list[EvalCase]:
    paths, suite = _iter_paths(name_or_path)
    cases: list[EvalCase] = []
    for path in paths:
        raw = yaml.safe_load(path.read_text()) or []
        items = raw["cases"] if isinstance(raw, dict) and "cases" in raw else raw
        for i, item in enumerate(items):
            cid = item.get("id") or f"{path.stem}-{i + 1}"
            case = EvalCase.from_raw(item, id=cid)
            case.metadata.setdefault("suite", suite)
            case.metadata.setdefault("source", path.name)
            cases.append(case)
    return cases
