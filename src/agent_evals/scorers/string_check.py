"""#22 String Check / Must-Contain — fraction of substring constraints satisfied
(required substrings present + forbidden substrings absent), case-insensitive."""

from __future__ import annotations

from ..core.scorer import Family, Score, ScorerSpec, ScoringContext, TurnScope


class StringCheck:
    spec = ScorerSpec(
        metric="string_check",
        number=22,
        title="String Check / Must-Contain",
        family=Family.DETERMINISTIC,
        turn_scope=TurnScope.SINGLE,
        needs_golden=True,
        requires_fields=["assistant_text"],
    )

    def score(self, ctx: ScoringContext) -> Score:
        must = ctx.expectations.response_must_contain or []
        forbidden = ctx.expectations.forbidden_substrings or []
        if not must and not forbidden:
            return Score.skip(self.spec.metric, "no response_must_contain / forbidden_substrings")

        text = (ctx.run.assistant_text or "").lower()
        missing = [t for t in must if t.lower() not in text]
        present_forbidden = [t for t in forbidden if t.lower() in text]

        total = len(must) + len(forbidden)
        satisfied = (len(must) - len(missing)) + (len(forbidden) - len(present_forbidden))
        value = satisfied / total if total else 1.0
        return Score(
            metric=self.spec.metric,
            value=value,
            rationale=f"missing={missing} forbidden_present={present_forbidden}",
            details={"missing": missing, "forbidden_present": present_forbidden},
        ).with_threshold(1.0)
