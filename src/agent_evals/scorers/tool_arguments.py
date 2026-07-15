"""#3 Tool Argument Correctness — observed args satisfy the expected spec per
tool. A spec value is either a LITERAL (exact equality — backward compatible)
or a structural MATCHER, so args can be asserted by SHAPE without pinning
environment data: requisition/recruiter ids and merged skill lists stay
unpinned while their structure (present, type, size, enum, pattern,
contains-item) is still enforced. This keeps #3 data-independent — matchers are
derived from the backend's tool input records ("code wins") and from values the
user literally provided in the conversation.

``expected_tool_args`` maps ``tool name -> {arg key -> expected}``:

- a plain scalar/list → exact equality (legacy semantics);
- a dict whose keys ALL start with ``$`` → a matcher; multiple ``$`` ops in one
  dict must all hold (AND);
- a dict with NO ``$`` keys → a nested SUBSET pattern: each listed field must
  match recursively, extra observed fields are tolerated;
- mixing ``$`` and plain keys in one dict is invalid and fails with a reason.

Supported matchers::

    {"$exists": true|false}          key present & non-null (false → absent/null)
    {"$eq": v}                       explicit exact equality
    {"$in": [v1, v2, ...]}           enum membership
    {"$type": "string"|"integer"|"number"|"boolean"|"array"|"object"|"null"}
    {"$regex": "pattern"}            re.search over a string value
    {"$size": {"min": n, "max": m}}  length bounds (arrays/strings/objects)
    {"$contains": M}                 array: at least one item matches M
    {"$contains_all": [M1, M2, ..]}  array: every Mi matched by >= 1 item

An empty spec ``{}`` for a tool asserts only that the tool was called (with any
args). A tool in the spec that was never called counts as missed.
"""

from __future__ import annotations

import re

from ..core.scorer import Family, Score, ScorerSpec, ScoringContext, TurnScope

_MISSING = object()

_SIMPLE_TYPES = {"string": str, "boolean": bool, "array": list, "object": dict,
                 "null": type(None)}


def _type_ok(name: object, obs: object) -> bool:
    # bool is an int subclass in Python — keep integer/number strictly numeric
    if name == "number":
        return isinstance(obs, (int, float)) and not isinstance(obs, bool)
    if name == "integer":
        return isinstance(obs, int) and not isinstance(obs, bool)
    t = _SIMPLE_TYPES.get(name)  # type: ignore[arg-type]
    return t is not None and isinstance(obs, t)


def _apply_matchers(spec: dict, obs: object, path: str) -> tuple[bool, str | None]:
    for op, arg in spec.items():
        if op == "$exists":
            present = obs is not _MISSING and obs is not None
            if bool(arg) != present:
                return False, f"{path}: exists={present}, expected exists={bool(arg)}"
            if not arg:
                continue  # asserted absent — no further op can apply
        elif obs is _MISSING or obs is None:
            return False, f"{path}: missing/null (required by {op})"
        elif op == "$eq":
            if obs != arg:
                return False, f"{path}: {obs!r} != {arg!r}"
        elif op == "$in":
            if obs not in (arg or []):
                return False, f"{path}: {obs!r} not in {arg!r}"
        elif op == "$type":
            if not _type_ok(arg, obs):
                return False, f"{path}: type {type(obs).__name__} is not {arg!r}"
        elif op == "$regex":
            if not isinstance(obs, str):
                return False, f"{path}: $regex needs a string, got {type(obs).__name__}"
            try:
                if not re.search(arg, obs):
                    return False, f"{path}: {obs!r} does not match /{arg}/"
            except re.error as exc:
                return False, f"{path}: invalid $regex {arg!r}: {exc}"
        elif op == "$size":
            try:
                ln = len(obs)  # type: ignore[arg-type]
            except TypeError:
                return False, f"{path}: $size needs a sized value, got {type(obs).__name__}"
            lo, hi = (arg or {}).get("min"), (arg or {}).get("max")
            if lo is not None and ln < lo:
                return False, f"{path}: size {ln} < min {lo}"
            if hi is not None and ln > hi:
                return False, f"{path}: size {ln} > max {hi}"
        elif op == "$contains":
            if not isinstance(obs, list):
                return False, f"{path}: $contains needs an array, got {type(obs).__name__}"
            if not any(match_value(arg, item, f"{path}[]")[0] for item in obs):
                return False, f"{path}: no item matches {arg!r}"
        elif op == "$contains_all":
            if not isinstance(obs, list):
                return False, f"{path}: $contains_all needs an array, got {type(obs).__name__}"
            for m in (arg or []):
                if not any(match_value(m, item, f"{path}[]")[0] for item in obs):
                    return False, f"{path}: no item matches {m!r}"
        else:
            return False, f"{path}: unknown matcher {op!r}"
    return True, None


def match_value(expected: object, observed: object, path: str = "$") -> tuple[bool, str | None]:
    """Match one observed value against a literal / matcher / subset pattern.
    Returns ``(ok, failure_reason)``; the reason pinpoints the first mismatch."""
    if isinstance(expected, dict) and expected:
        dollar = [k for k in expected if isinstance(k, str) and k.startswith("$")]
        if dollar:
            if len(dollar) != len(expected):
                return False, f"{path}: spec mixes matcher and plain keys {sorted(expected)}"
            return _apply_matchers(expected, observed, path)
        # nested subset pattern over an object
        if not isinstance(observed, dict):
            return False, f"{path}: expected an object, got {type(observed).__name__}"
        for k, v in expected.items():
            ok, why = match_value(v, observed.get(k, _MISSING), f"{path}.{k}")
            if not ok:
                return ok, why
        return True, None
    if observed is _MISSING:
        return False, f"{path}: missing"
    # literals (incl. empty dict): exact equality
    if observed != expected:
        return False, f"{path}: {observed!r} != {expected!r}"
    return True, None


class ToolArgumentCorrectness:
    spec = ScorerSpec(
        metric="tool_argument_correctness",
        number=3,
        title="Tool Argument Correctness",
        family=Family.DETERMINISTIC,
        turn_scope=TurnScope.SINGLE,
        needs_golden=True,
        requires_fields=["tool_calls"],
    )

    def score(self, ctx: ScoringContext) -> Score:
        expected = ctx.expectations.expected_tool_args
        if not expected:
            return Score.skip(self.spec.metric, "no expected_tool_args in expectations")

        calls_by_name: dict[str, list[dict]] = {}
        for tc in ctx.run.tool_calls:
            calls_by_name.setdefault(tc.name or "", []).append(tc.args or {})

        matched: list[str] = []
        missed: list[str] = []
        reasons: dict[str, str] = {}
        for tool_name, want_args in expected.items():
            candidates = calls_by_name.get(tool_name, [])
            if not candidates:
                missed.append(tool_name)
                reasons[tool_name] = "tool not called"
                continue
            ok = False
            last_reason: str | None = None
            for obs in candidates:  # any call of this tool may satisfy the spec
                good = True
                for key, want in (want_args or {}).items():
                    got = obs.get(key, _MISSING) if isinstance(obs, dict) else _MISSING
                    r, why = match_value(want, got, path=key)
                    if not r:
                        good, last_reason = False, why
                        break
                if good:
                    ok = True
                    break
            if ok:
                matched.append(tool_name)
            else:
                missed.append(tool_name)
                reasons[tool_name] = last_reason or "no call satisfied the spec"

        value = len(matched) / len(expected)
        return Score(
            metric=self.spec.metric,
            value=value,
            rationale=f"matched={matched} missed={missed}",
            details={"matched": matched, "missed": missed, "failure_reasons": reasons},
        ).with_threshold(1.0)
