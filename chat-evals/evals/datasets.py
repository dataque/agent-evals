"""
Example evaluation datasets.

Replace these with datasets for the agent you are evaluating, then register
them in ``ALL_DATASETS``. Each item is either single-turn or multi-turn:

Single-turn:
    {"inputs": {"question": str}, "expectations": {...}}

Multi-turn:
    {"inputs": {"scenario": str, "turns": [
        {"question": str, "expectations": {...}},
        ...
    ]}}

Multi-turn items share a contextId across turns (LocalBenchmarker). In AICE
mode, multi-turn items are flattened to independent single-turn items.

Supported expectation fields (all optional — each scorer skips when its field
is absent):
  - expected_response     : reference answer for the Correctness judge
  - response_must_contain : substrings the output must include
  - expected_tool_calls   : tool names the agent should call (tool_trace_f1)
  - expected_tool_args    : {tool: {arg: value}} expected call args
  - max_steps             : step budget for step_efficiency
  - expected_routes       : allowed sub-agent ids for plan_quality
  - allowed_tool_calls    : allowed tool names for plan_quality
  - expected_actions      : mutating tools that must complete with status ok
  - expected_artifacts    : {name: schema_id} artifacts the agent must emit
"""

from __future__ import annotations

EXAMPLE_DATASET = [
    # ------------------------------------------------------------------
    # Single-turn: factual question with a reference answer + keyword check
    # ------------------------------------------------------------------
    {
        "inputs": {"question": "What is the capital of France?"},
        "expectations": {
            "expected_response": "The capital of France is Paris.",
            "response_must_contain": ["Paris"],
        },
    },

    # ------------------------------------------------------------------
    # Single-turn: open-ended capability question (relevance / tone only)
    # ------------------------------------------------------------------
    {
        "inputs": {"question": "What can you help me with?"},
        "expectations": {
            "expected_response": (
                "A brief summary of the assistant's capabilities, inviting the "
                "user to ask a question."
            ),
        },
    },

    # ------------------------------------------------------------------
    # Single-turn with trace expectations (for tool-using agents that emit
    # an execution_trace artifact — see schemas/a2a_response.v1.json)
    # ------------------------------------------------------------------
    {
        "inputs": {"question": "What's the weather in Paris right now?"},
        "expectations": {
            "expected_tool_calls": ["get_weather"],
            "expected_tool_args": {"get_weather": {"city": "Paris"}},
            "max_steps": 3,
        },
    },

    # ------------------------------------------------------------------
    # Multi-turn: a short conversation sharing one contextId
    # ------------------------------------------------------------------
    {
        "inputs": {
            "scenario": "recommendation_followup",
            "turns": [
                {
                    "question": "Recommend a good introductory book on machine learning.",
                    "expectations": {
                        "response_must_contain": ["book"],
                    },
                },
                {
                    "question": "Why did you pick that one?",
                    "expectations": {
                        "expected_response": (
                            "A justification that references the book recommended "
                            "in the previous turn."
                        ),
                    },
                },
            ],
        },
    },
]


# ---------------------------------------------------------------------------
# Aggregate all datasets. Add an entry per agent/dataset you want to evaluate;
# the key becomes a valid value for the --agent flag.
# ---------------------------------------------------------------------------
ALL_DATASETS: dict[str, list[dict]] = {
    "example": EXAMPLE_DATASET,
}


def get_dataset(agent_name: str | None = None) -> list[dict]:
    """Return the eval dataset for a specific agent, or all datasets merged."""
    if agent_name:
        if agent_name not in ALL_DATASETS:
            raise ValueError(
                f"Unknown agent '{agent_name}'. Available: {list(ALL_DATASETS.keys())}"
            )
        return ALL_DATASETS[agent_name]
    # Return all merged
    merged = []
    for name, ds in ALL_DATASETS.items():
        for item in ds:
            item_copy = {**item}
            item_copy.setdefault("metadata", {})
            item_copy["metadata"]["agent"] = name
            merged.append(item_copy)
    return merged
