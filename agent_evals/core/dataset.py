"""Dataset format.

Each dataset item is single-turn or multi-turn — same shape as chat-evals.

Single-turn::

    {"inputs": {"question": str}, "expectations": {...}}

Multi-turn (turns share a thread_id when the runner is multi-turn-capable)::

    {"inputs": {"scenario": str, "turns": [
        {"question": str, "expectations": {...}},
        ...
    ]}}

The ``expectations`` dict carries per-row golden values consumed by scorers:
``expected_response``, ``response_must_contain``, ``expected_tool_calls``,
``expected_tool_args``, ``expected_routes``, ``allowed_tool_calls``,
``max_steps``, ``expected_actions``, ``expected_artifacts``. Scorers return
``None`` when their expectation key is missing — the row is skipped for that
metric, so datasets can mix expectations per item without breaking aggregation.
"""

from __future__ import annotations

from typing import Any, TypeAlias

DatasetItem: TypeAlias = dict[str, Any]
Dataset: TypeAlias = list[DatasetItem]


def flatten_multi_turn(dataset: Dataset) -> Dataset:
    """Flatten multi-turn items into independent single-turn items.

    Used by single-turn-only runners. Each turn becomes its own item with no
    shared context (matches chat-evals' ``_flatten_multi_turn``).
    """
    flat: Dataset = []
    for item in dataset:
        inputs = item.get("inputs", {})
        if "turns" in inputs:
            for turn in inputs["turns"]:
                flat.append(
                    {
                        "inputs": {"question": turn["question"]},
                        "expectations": turn.get("expectations", {}),
                    }
                )
        else:
            flat.append(item)
    return flat
