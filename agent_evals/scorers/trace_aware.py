"""Trace-aware custom scorers.

These scorers consume the ``trace`` column produced by the runner from the
hr-agent v1 ``execution_trace`` artifact (or any future protocol adapter that
emits a compatible trace). Each scorer wraps the row's trace dict in a
:class:`~agent_evals.core.trace.Trace` to use its accessor helpers.

Ported from chat-evals/evals/scorers.py:112-247.
"""

from __future__ import annotations

from typing import Any

from agent_evals.core.scorer import scorer
from agent_evals.core.trace import Trace, f1_score


def _wrap(trace: Any) -> Trace:
    """Coerce a row's ``trace`` column (dict or Trace) into a Trace object."""
    if isinstance(trace, Trace):
        return trace
    return Trace.from_dict(trace)


# ---------------------------------------------------------------------------
# Tool-trajectory scorers
# ---------------------------------------------------------------------------


@scorer
def tool_trace_f1(expectations: dict, trace: Any) -> float | None:
    """F1 of observed tool-call names against ``expectations.expected_tool_calls``."""
    expected = expectations.get("expected_tool_calls")
    if expected is None:
        return None
    expected_set = {str(t) for t in expected}
    t = _wrap(trace)
    observed_set = {
        (e.get("data", {}) or {}).get("tool_name", "")
        for e in t.tool_calls()
    }
    observed_set.discard("")
    return f1_score(expected_set, observed_set)


@scorer
def tool_argument_correctness(expectations: dict, trace: Any) -> float | None:
    """Fraction of expected tool calls whose observed args match.

    ``expectations.expected_tool_args`` is ``{tool_name: {arg: value, ...}}``.
    A tool is "correct" if every key/value in the expectation appears in the
    observed args (subset match — extra observed args are tolerated).
    """
    expected = expectations.get("expected_tool_args")
    if not expected:
        return None
    t = _wrap(trace)
    calls_by_name: dict[str, list[dict]] = {}
    for ev in t.tool_calls():
        name = (ev.get("data", {}) or {}).get("tool_name", "")
        calls_by_name.setdefault(name, []).append(
            (ev.get("data", {}) or {}).get("args", {}) or {}
        )

    matches = 0
    for tool_name, want_args in expected.items():
        candidates = calls_by_name.get(tool_name, [])
        if any(
            all(observed.get(k) == v for k, v in (want_args or {}).items())
            for observed in candidates
        ):
            matches += 1
    return matches / len(expected)


@scorer
def step_efficiency(expectations: dict, trace: Any) -> float | None:
    """Observed step count vs ``expectations.max_steps`` (1.0 = at-or-under budget)."""
    max_steps = expectations.get("max_steps")
    if max_steps is None:
        return None
    t = _wrap(trace)
    observed = len(t.events())
    if observed == 0:
        return 1.0
    if observed <= int(max_steps):
        return 1.0
    return max(0.0, int(max_steps) / observed)


@scorer
def plan_quality(expectations: dict, trace: Any) -> float | None:
    """Routing + tool-set are within the expected envelope.

    ``expectations.expected_routes`` is a list of allowed sub-agent ids.
    ``expectations.allowed_tool_calls`` is a list of allowed tool names.
    Returns the average of (routes within allowed) and (tools within allowed).
    """
    expected_routes = expectations.get("expected_routes")
    allowed_tools = expectations.get("allowed_tool_calls")
    if expected_routes is None and allowed_tools is None:
        return None

    t = _wrap(trace)
    parts: list[float] = []
    if expected_routes is not None:
        allowed = {str(r) for r in expected_routes}
        observed_routes = {
            (e.get("data", {}) or {}).get("route_to", "") for e in t.routes()
        }
        observed_routes.discard("")
        if not observed_routes:
            parts.append(1.0 if not allowed else 0.0)
        else:
            parts.append(len(observed_routes & allowed) / len(observed_routes))

    if allowed_tools is not None:
        allowed = {str(x) for x in allowed_tools}
        observed_tools = {
            (e.get("data", {}) or {}).get("tool_name", "") for e in t.tool_calls()
        }
        observed_tools.discard("")
        if not observed_tools:
            parts.append(1.0 if not allowed else 0.0)
        else:
            parts.append(len(observed_tools & allowed) / len(observed_tools))

    return sum(parts) / len(parts) if parts else None


@scorer
def audit_log_action_taken(expectations: dict, trace: Any) -> float | None:
    """Fraction of ``expectations.expected_actions`` that ran with status ``ok``.

    An "action" is the name of a mutating tool (e.g. ``save_skills``,
    ``send_message``). The action passes if a ``tool_result`` event with that
    tool name and ``status == "ok"`` exists.
    """
    expected = expectations.get("expected_actions")
    if not expected:
        return None
    t = _wrap(trace)
    ok_results = {
        (e.get("data", {}) or {}).get("tool_name", "")
        for e in t.tool_results()
        if (e.get("data", {}) or {}).get("status") == "ok"
    }
    matched = sum(1 for a in expected if a in ok_results)
    return matched / len(expected)


# ---------------------------------------------------------------------------
# Artifact-aware scorer (A2A; for ag-ui projects, use schema_adherence.py)
# ---------------------------------------------------------------------------


@scorer
def card_format_correctness(expectations: dict, artifacts: Any) -> float | None:
    """Each expected artifact name was produced with the expected schema id.

    ``expectations.expected_artifacts`` is ``{name: schema_id}``. Skip when not set.

    For ag-ui projects (where artifacts have no ``schema`` field on the wire),
    use ``agent_evals.scorers.schema_adherence.tool_result_schema_adherence``
    instead — it validates the payload against a project-supplied JSON Schema.
    """
    expected = expectations.get("expected_artifacts")
    if not expected:
        return None
    if not isinstance(artifacts, dict):
        return 0.0
    matched = 0
    for name, want_schema in expected.items():
        data = artifacts.get(name)
        if isinstance(data, dict) and data.get("schema") == want_schema:
            matched += 1
    return matched / len(expected)
