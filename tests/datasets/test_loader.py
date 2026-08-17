"""The bundled HR suite loads into well-formed EvalCases."""

from __future__ import annotations

from pathlib import Path

import pytest

import agent_evals.datasets as _ds
from agent_evals.datasets import load_suite, suite_fingerprint

# The bundled HR suite (datasets/hr/*.yaml) is gitignored / not shipped in the repo;
# skip suite-dependent tests when it isn't present locally (e.g. a fresh clone).
_HR_SUITE = Path(_ds.__file__).parent / "hr"
pytestmark = pytest.mark.skipif(
    not (_HR_SUITE.is_dir() and any(_HR_SUITE.glob("*.y*ml"))),
    reason="bundled hr suite not present (gitignored) — provide a suite to exercise these",
)


def test_load_bundled_hr_suite():
    cases = load_suite("hr")
    by_id = {c.id: c for c in cases}

    # talent-profile cluster: suggesting never saves; skill edits are now a chat
    # capability (edit_skills stages session state; Confirm → save_skills
    # persists) — the old deflect behaviour is retired for SKILL edits.
    assert by_id["suggest-skills-figma-pill"].expectations.expected_tool_calls == ["suggest_skills"]
    assert by_id["suggest-skills-figma-pill"].expectations.expected_actions == []
    assert by_id["save-skills-confirm-trigger"].expectations.expected_actions == ["save_skills"]
    assert "edit_skills" in by_id["save-skills-confirm-trigger"].expectations.expected_tool_calls
    # edit references existing skills; the pre-edit get_skills read is
    # non-deterministic, so it's scored as optional (not required, not penalized).
    assert by_id["edit-skills-conversational"].expectations.expected_tool_calls == ["edit_skills"]
    assert by_id["edit-skills-conversational"].expectations.optional_tool_calls == ["get_skills"]
    assert by_id["edit-skills-conversational"].expectations.expected_actions == []

    # requisitions cluster: precondition tag parses.
    assert by_id["find-roles-canonical-matches"].requires == ["has_matched_requisitions"]

    # the role chain (view / Q&A / draft) needs a role in context → multi-turn journeys.
    chain = by_id["journey-role-chain-happy"]
    assert chain.is_multi_turn and chain.requires == ["has_matched_requisitions"]
    draft_turn = chain.as_turns()[3]
    assert draft_turn.expectations.expected_tool_calls == ["draft_message"]
    # post pills-refactor: scenario ids are the backend NextSteps scenario names
    # (derivational metadata); the scored golden is the pill set.
    assert draft_turn.expectations.expected_scenario_id == "DraftComplete"
    assert draft_turn.expectations.expected_pills == [
        "How can I apply to a role?", "What else can you help me with?"]
    assert by_id["journey-role-qa-hiring-manager"].as_turns()[-1].expectations.must_refuse is True

    # metadata stamped by the loader
    assert all(c.metadata.get("suite") == "hr" for c in cases)


def test_suite_fingerprint_identifies_the_dataset_bytes(tmp_path):
    """The dataset is untracked, so a run's provenance depends on this (E5)."""
    fp = suite_fingerprint("hr")
    assert fp["suite"] == "hr"
    assert fp["case_count"] == len(load_suite("hr"))
    assert set(fp["files"]) == {p.name for p in _HR_SUITE.glob("*.y*ml")}
    assert suite_fingerprint("hr")["digest"] == fp["digest"], "must be stable"

    # any edit to any suite file moves the digest
    (tmp_path / "a.yaml").write_text("- id: x\n  inputs: {question: hi}\n")
    before = suite_fingerprint(str(tmp_path))["digest"]
    (tmp_path / "a.yaml").write_text("- id: x\n  inputs: {question: hi there}\n")
    assert suite_fingerprint(str(tmp_path))["digest"] != before
    # as does adding one, even with the other file untouched
    after_edit = suite_fingerprint(str(tmp_path))["digest"]
    (tmp_path / "b.yaml").write_text("- id: y\n  inputs: {question: yo}\n")
    assert suite_fingerprint(str(tmp_path))["digest"] != after_edit


def test_bundled_hr_suite_has_no_golden_gaps():
    """Every golden-driven metric should have at least one bundled case that
    exercises the field it needs. ``remembered_facts`` (#11) and ``other_user_id``
    (#8) are PENDING until the journeys (#27) and adversarial (#26) clusters land."""
    cases = load_suite("hr")
    seen_exp: set[str] = set()
    seen_meta: set[str] = set()
    # expected_response is intentionally NOT used — goldens are data-independent
    # (no pinned answers), so answer_equivalence (#6) is retired for this eval.
    # expected_tool_args (#3) is back: structural $-matcher goldens derived from
    # the backend tool input records + user-provided values (no pinned env data).
    golden_fields = (
        "response_must_contain", "forbidden_substrings",
        "expected_tool_calls", "expected_tool_args", "allowed_tool_calls",
        "expected_pills", "expected_actions", "max_steps", "expected_routes",
        "remembered_facts", "must_refuse", "expected_redirect", "other_user_id",
        "rubric",
    )
    for c in cases:
        seen_meta |= set(c.metadata)
        for turn in c.as_turns():
            for field in golden_fields:
                if getattr(turn.expectations, field) is not None:
                    seen_exp.add(field)

    for field in ("expected_tool_args", "rubric", "must_refuse", "expected_routes", "expected_pills"):
        assert field in seen_exp, f"golden gap remains: {field}"
    assert "user_feedback" in seen_meta, "golden gap remains: metadata.user_feedback (#23)"

    # every golden field is now exercised by some bundled case (clusters #26 + #27
    # closed the last gaps: other_user_id and remembered_facts).
    missing = {f for f in golden_fields if f not in seen_exp}
    assert not missing, f"golden fields with no example: {sorted(missing)}"
