"""Compare multiple judge backends on the same input (judge A/B benchmarking)."""

from __future__ import annotations

from ..core.judge import Judge, JudgeVerdict


def compare_judges(judges: list[Judge], *, criteria: str, response: str, **kwargs) -> dict[str, JudgeVerdict]:
    """Run each judge on identical inputs; return ``{judge_name: verdict}``."""
    return {j.name: j.evaluate(criteria=criteria, response=response, **kwargs) for j in judges}
