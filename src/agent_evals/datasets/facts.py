"""Per-run data-fact derivation for the HR agent (environment-independent eval).

The eval runs against dev/UAT/prod whose databases differ, so a case's
applicability and its data-dependent branch must be decided from what the agent's
tools actually returned THIS run — never from frozen/seeded data or a static
config. The runner calls a ``derive_facts(runs) -> dict`` (wired in by the CLI)
and checks each case's ``requires:`` against it; an unmet precondition skips the
case with a reported reason.

These rules are specific to the HR agent's tools; another agent supplies its own
deriver. Nothing in ``core`` imports this — it stays framework-neutral.
"""

from __future__ import annotations

from ..core.run_record import RunRecord


def _results_of(runs: list[RunRecord], name: str) -> list:
    return [tc.result for r in runs for tc in r.tool_calls if tc.name == name]


def derive_hr_facts(runs: list[RunRecord]) -> dict:
    """Read data-facts from a case's runs. A fact is set only when the relevant
    tool actually ran this turn, so an absent fact means "not observed in this
    environment" (and a ``requires:`` on it will skip the case)."""
    facts: dict = {}

    sugg = _results_of(runs, "suggest_requisitions")
    if sugg:
        has = any(isinstance(res, dict) and res.get("matches") for res in sugg)
        facts["has_matched_requisitions"] = bool(has)
        facts["no_matched_requisitions"] = not has

    # NOTE: there is deliberately no `has_skills` fact. It could only be derived
    # from a `get_skills` run, because suggest_skills/edit_skills overwrite the
    # same state property with inferred or staged skills rather than saved ones.
    # `get_skills` was removed from the product, so the fact became underivable
    # and any `requires: has_skills` would skip unconditionally (E10).

    # Profile completeness from the analyze_talent_profile result itself (the
    # old profile_analyzed / profile_not_set_up pill scenarios are gone with the
    # pills refactor). Mirror the backend's own threshold: >= 4 missing sections
    # is "mostly empty" (profile_sparse).
    for res in _results_of(runs, "analyze_talent_profile"):
        if isinstance(res, dict) and isinstance(res.get("missingSections"), list):
            sparse = len(res["missingSections"]) >= 4
            facts["profile_complete"], facts["profile_sparse"] = (not sparse), sparse

    return facts
