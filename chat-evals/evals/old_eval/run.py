#!/usr/bin/env python3
"""
Run evaluation scenarios against agents.

Usage:
    python -m eval.run [SCENARIO_PATH]
    python -m eval.run eval/scenarios/mycareer_happy_flow.json

Requirements:
    - AZURE_OPENAI_API_KEY, AZURE_OPENAI_ENDPOINT (or your LLM env vars)
    - Run from project root or pass absolute scenario path
    - Profile: set PROFILE_PATH or rely on the scenario's profile_path
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

# Add project root before importing app modules
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger("eval.run")


def _load_scenario(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _apply_scenario_profile(scenario: dict) -> None:
    """Set PROFILE_PATH from scenario so load_profile() uses the right data."""
    profile_path = scenario.get("profile_path")
    if not profile_path:
        return
    abs_path = os.path.join(_PROJECT_ROOT, profile_path)
    if os.path.exists(abs_path):
        os.environ["PROFILE_PATH"] = abs_path
        logger.info("Using profile: %s", abs_path)
    else:
        logger.warning("Scenario profile_path not found: %s", abs_path)


class EvalAgentAdapter:
    """Wraps an agent so EvalRunner's invoke(user_input, thread_id=...) works."""

    def __init__(self, agent, context_factory=None):
        self._agent = agent
        self._context_factory = context_factory or self._default_context

    def _default_context(self, thread_id: str):
        from core.state import BaseContext
        return BaseContext(thread_id=thread_id)

    async def invoke(self, user_input: str, thread_id: str = "eval") -> dict:
        ctx = self._context_factory(thread_id)
        return await self._agent.invoke(user_input, context=ctx)


def _get_agent_for_scenario(scenario: dict, checkpointer=None):
    """Build the agent specified by the scenario."""
    agent_name = scenario.get("agent", "profile")
    catalog = __import__("agents.catalog", fromlist=["build_agent_catalog"]).build_agent_catalog
    registry = catalog(checkpointer=checkpointer)
    agent = registry.get(agent_name)
    if agent is None:
        raise ValueError(f"Unknown agent: {agent_name}")
    return agent


def main() -> int:
    parser = argparse.ArgumentParser(description="Run evaluation scenarios")
    parser.add_argument(
        "scenario",
        nargs="?",
        default=os.path.join(_PROJECT_ROOT, "eval", "scenarios", "mycareer_happy_flow.json"),
        help="Path to scenario JSON file",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    args = parser.parse_args()

    scenario_path = args.scenario
    if not os.path.isabs(scenario_path):
        scenario_path = os.path.join(_PROJECT_ROOT, scenario_path)
    if not os.path.exists(scenario_path):
        logger.error("Scenario file not found: %s", scenario_path)
        return 1

    scenario = _load_scenario(scenario_path)
    _apply_scenario_profile(scenario)

    from langgraph.checkpoint.memory import InMemorySaver
    from core.eval.runner import EvalRunner

    checkpointer = InMemorySaver()
    agent = _get_agent_for_scenario(scenario, checkpointer=checkpointer)

    # Use scenario's context_factory if agent has one (e.g. MyCareerContext)
    context_factory = getattr(agent.config, "context_factory", None)
    adapter = EvalAgentAdapter(agent, context_factory=context_factory)
    runner = EvalRunner(agent=adapter)

    async def run():
        return await runner.run_scenario_file(scenario_path)

    result = asyncio.run(run())
    print(result.summary())
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
