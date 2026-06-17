"""A deterministic, no-LLM judge.

Useful as (a) an offline test double for judged-scorer wiring, and (b) a
graceful fallback when no LLM judge is configured. It is NOT a substitute for a
real judge in production — its "verdicts" are crude heuristics.
"""

from __future__ import annotations

from ..core.judge import Judge, JudgeVerdict


class HeuristicJudge:
    name = "heuristic"

    def __init__(self, *, fixed_score: float | None = None) -> None:
        self.fixed_score = fixed_score

    def evaluate(
        self,
        *,
        criteria: str,
        response: str,
        question: str | None = None,
        context: str | None = None,
        reference: str | None = None,
    ) -> JudgeVerdict:
        if self.fixed_score is not None:
            s = max(0.0, min(1.0, self.fixed_score))
            return JudgeVerdict(score=s, passed=s >= 0.5, rationale="fixed heuristic score")
        resp = (response or "").lower()
        if reference:
            ref_words = {w for w in reference.lower().split() if len(w) > 3}
            if ref_words:
                overlap = sum(1 for w in ref_words if w in resp) / len(ref_words)
                return JudgeVerdict(score=overlap, passed=overlap >= 0.5,
                                    rationale=f"{overlap:.0%} reference-keyword overlap")
        val = 1.0 if resp.strip() else 0.0
        return JudgeVerdict(score=val, passed=bool(resp.strip()),
                            rationale="non-empty-response heuristic")


# Sanity: HeuristicJudge satisfies the Judge protocol.
_: Judge = HeuristicJudge()
