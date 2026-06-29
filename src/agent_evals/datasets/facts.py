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


def _emitted_scenarios(runs: list[RunRecord]) -> set[str]:
    out: set[str] = set()
    for r in runs:
        for tc in r.tool_calls:
            res = tc.result
            if isinstance(res, dict):
                sid = res.get("scenarioId") or res.get("scenario_id")
                if sid:
                    out.add(str(sid))
    return out


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

    skills = _results_of(runs, "get_skills")
    if skills:
        facts["has_skills"] = any(isinstance(res, dict) and res.get("top") for res in skills)

    scenarios = _emitted_scenarios(runs)
    if "profile_analyzed" in scenarios:
        facts["profile_complete"], facts["profile_sparse"] = True, False
    elif "profile_not_set_up" in scenarios:
        facts["profile_complete"], facts["profile_sparse"] = False, True

    return facts
