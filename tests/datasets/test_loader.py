"""The bundled HR suite loads into well-formed EvalCases."""

from __future__ import annotations

from agent_evals.datasets import load_suite


def test_load_bundled_hr_suite():
    cases = load_suite("hr")
    by_id = {c.id: c for c in cases}

    assert "modify-and-save-skills" in by_id
    multi = by_id["modify-and-save-skills"]
    assert multi.is_multi_turn and len(multi.as_turns()) == 3
    assert multi.as_turns()[1].expectations.expected_actions == ["save_skills"]
    assert multi.as_turns()[2].expectations.remembered_facts == ["Java", "React"]

    assert by_id["out-of-scope-refusal"].expectations.must_refuse is True
    assert by_id["cross-user-isolation-probe"].expectations.other_user_id == "00009999"
    assert by_id["cross-user-isolation-probe"].expectations.must_refuse is True
    assert by_id["suggest-skills"].expectations.expected_tool_calls == ["suggest_skills"]
    # metadata stamped by the loader
    assert all(c.metadata.get("suite") == "hr" for c in cases)


def test_bundled_hr_suite_has_no_golden_gaps():
    """Every golden-driven metric must have at least one bundled case that
    exercises the field it needs (otherwise that scorer always skips)."""
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

    # the four fields that previously had zero coverage are now present
    for field in ("expected_tool_args", "expected_response", "rubric"):
        assert field in seen_exp, f"golden gap remains: {field}"
    assert "user_feedback" in seen_meta, "golden gap remains: metadata.user_feedback (#23)"

    # and every golden field is covered → no scorer skips purely for lack of data
    missing = [f for f in golden_fields if f not in seen_exp]
    assert not missing, f"golden fields with no example: {missing}"
