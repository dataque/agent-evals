"""The bundled HR suite loads into well-formed EvalCases."""

from __future__ import annotations

from agent_evals.datasets import load_suite


def test_load_bundled_hr_suite():
    cases = load_suite("hr")
    by_id = {c.id: c for c in cases}

    # talent-profile cluster: suggesting never saves; the Figma conversational
    # edit must DEFLECT (no tool, no save) per the current backend prompt.
    assert by_id["suggest-skills-figma-pill"].expectations.expected_tool_calls == ["suggest_skills"]
    assert by_id["suggest-skills-figma-pill"].expectations.expected_actions == []
    assert by_id["save-skills-confirm-trigger"].expectations.expected_actions == ["save_skills"]
    assert by_id["save-skills-conversational-edit-must-deflect"].expectations.expected_tool_calls == []

    # requisitions cluster: precondition tag parses.
    assert by_id["find-roles-canonical-matches"].requires == ["has_matched_requisitions"]

    # the role chain (view / Q&A / draft) needs a role in context → multi-turn journeys.
    chain = by_id["journey-role-chain-happy"]
    assert chain.is_multi_turn and chain.requires == ["has_matched_requisitions"]
    draft_turn = chain.as_turns()[3]
    assert draft_turn.expectations.expected_tool_calls == ["draft_message"]
    assert draft_turn.expectations.expected_scenario_id == "draft_complete"
    assert by_id["journey-role-qa-hiring-manager"].as_turns()[-1].expectations.must_refuse is True

    # metadata stamped by the loader
    assert all(c.metadata.get("suite") == "hr" for c in cases)


def test_bundled_hr_suite_has_no_golden_gaps():
    """Every golden-driven metric should have at least one bundled case that
    exercises the field it needs. ``remembered_facts`` (#11) and ``other_user_id``
    (#8) are PENDING until the journeys (#27) and adversarial (#26) clusters land."""
    cases = load_suite("hr")
    seen_exp: set[str] = set()
    seen_meta: set[str] = set()
    golden_fields = (
        "expected_response", "response_must_contain", "forbidden_substrings",
        "expected_tool_calls", "expected_tool_args", "allowed_tool_calls",
        "expected_actions", "max_steps", "expected_routes", "remembered_facts",
        "must_refuse", "expected_redirect", "other_user_id", "rubric",
    )
    for c in cases:
        seen_meta |= set(c.metadata)
        for turn in c.as_turns():
            for field in golden_fields:
                if getattr(turn.expectations, field) is not None:
                    seen_exp.add(field)

    for field in ("expected_tool_args", "expected_response", "rubric", "must_refuse", "expected_routes"):
        assert field in seen_exp, f"golden gap remains: {field}"
    assert "user_feedback" in seen_meta, "golden gap remains: metadata.user_feedback (#23)"

    pending = {"remembered_facts", "other_user_id"}  # land with the #27 / #26 clusters
    missing = {f for f in golden_fields if f not in seen_exp}
    assert missing <= pending, f"unexpected golden gap: {sorted(missing - pending)}"
