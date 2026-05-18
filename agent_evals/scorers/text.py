"""Text-based custom scorers."""

from __future__ import annotations

from agent_evals.core.scorer import scorer


@scorer
def response_completeness(
    expectations: dict,
    outputs: str,
) -> float | None:
    """Fraction of ``expectations.response_must_contain`` strings found in
    ``outputs`` (case-insensitive).

    Returns ``None`` when the row has no ``response_must_contain`` — the
    scorer is skipped for that row, so datasets can mix expectations per item
    without breaking aggregation.

    Ported from chat-evals/evals/scorers.py:94-105.
    """
    must_contain = expectations.get("response_must_contain")
    if not must_contain:
        return None
    output_lower = (outputs or "").lower()
    found = sum(1 for term in must_contain if term.lower() in output_lower)
    return found / len(must_contain)
