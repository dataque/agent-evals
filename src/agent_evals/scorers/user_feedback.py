"""#23 User Feedback Signal — operational. Surfaces per-message production
feedback (thumbs / rating / correction) when it is attached to a case.

Two paths: (a) feedback-annotated eval cases flow through this scorer in a
normal run (read from ``case.metadata['user_feedback']``); (b) bulk production
feedback is aggregated offline via ``agent_evals.feedback.ingest`` — see that
module. This is telemetry, not a model judgment.
"""

from __future__ import annotations

from ..core.scorer import Family, Score, ScorerSpec, ScoringContext, TurnScope

_POSITIVE = {"up", "1", "true", "positive", "good", "thumbs_up"}


def _rating_from(fb: dict) -> float | None:
    if "rating" in fb and fb["rating"] is not None:
        try:
            return max(0.0, min(1.0, float(fb["rating"])))
        except (TypeError, ValueError):
            return None
    thumbs = fb.get("thumbs")
    if thumbs is not None:
        return 1.0 if str(thumbs).lower() in _POSITIVE else 0.0
    return None


class UserFeedbackSignal:
    spec = ScorerSpec(
        metric="user_feedback_signal", number=23, title="User Feedback Signal",
        family=Family.OPERATIONAL, turn_scope=TurnScope.SINGLE,
    )

    def score(self, ctx: ScoringContext) -> Score:
        fb = ctx.case.metadata.get("user_feedback")
        if isinstance(fb, list):  # per-turn list
            fb = fb[ctx.turn_index] if ctx.turn_index < len(fb) else None
        if not fb:
            return Score.skip(self.spec.metric, "no user_feedback attached to case")
        rating = _rating_from(fb)
        if rating is None:
            return Score.skip(self.spec.metric, "feedback has no thumbs/rating")
        return Score(
            metric=self.spec.metric, value=rating,
            rationale=fb.get("correction") or f"thumbs={fb.get('thumbs')}",
            details=dict(fb),
        ).with_threshold(0.5)
