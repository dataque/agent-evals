"""Shared helpers for judged scorers: resolve the judge, build grounding
context from tool outputs, and turn a ``JudgeVerdict`` into a ``Score`` (treating
a judge backend failure as an errored score, not a real 0)."""

from __future__ import annotations

import json

from ..core.judge import Judge
from ..core.run_record import RunRecord
from ..core.scorer import Score, ScoringContext


def resolve_judge(scorer, ctx: ScoringContext) -> Judge | None:
    """Per-metric judge (injected at construction) wins; else the run default."""
    return getattr(scorer, "judge", None) or ctx.judge


def tool_context(run: RunRecord, *, limit: int = 2000) -> str:
    parts: list[str] = []
    for tc in run.tool_calls:
        if tc.result is not None:
            blob = json.dumps(tc.result, default=str)
            parts.append(f"[{tc.name}] {blob[:limit]}")
    return "\n".join(parts)


def transcript(runs: list[RunRecord], *, limit: int = 6000) -> str:
    """Render a multi-turn conversation for whole-conversation judges."""
    lines: list[str] = []
    for i, r in enumerate(runs):
        lines.append(f"User (turn {i + 1}): {r.user_message}")
        lines.append(f"Assistant (turn {i + 1}): {r.assistant_text or '(tool-only turn)'}")
    text = "\n".join(lines)
    return text[:limit]


def turn_context(ctx: ScoringContext) -> str | None:
    """Grounding for a SINGLE-turn judge so it doesn't misjudge multi-turn / tool
    turns: the conversation BEFORE this turn (so recall isn't read as fabrication)
    + the tool outputs this turn (so a tool-driven result isn't read as 'not done')."""
    parts: list[str] = []
    if ctx.turn_index > 0:
        parts.append("EARLIER IN THIS CONVERSATION:\n" + transcript(ctx.runs[: ctx.turn_index]))
    tools = tool_context(ctx.run)
    if tools:
        parts.append("TOOL OUTPUTS THIS TURN:\n" + tools)
    return "\n\n".join(parts) or None


def require_text(ctx: ScoringContext, metric: str) -> Score | None:
    if not (ctx.run.assistant_text or "").strip():
        return Score.skip(metric, "empty assistant text")
    return None


def judged(
    metric: str,
    judge: Judge,
    *,
    criteria: str,
    response: str,
    question: str | None = None,
    context: str | None = None,
    reference: str | None = None,
    threshold: float = 0.5,
    extra: dict | None = None,
) -> Score:
    v = judge.evaluate(
        criteria=criteria, response=response, question=question,
        context=context, reference=reference,
    )
    if isinstance(v.raw, dict) and v.raw.get("error"):
        return Score.failed(metric, str(v.raw["error"]), judge=getattr(judge, "name", ""))
    score = max(0.0, min(1.0, float(v.score)))
    s = Score(metric=metric, value=score, rationale=v.rationale,
              details={"judge": getattr(judge, "name", ""), **(extra or {})})
    if v.passed is not None:
        s.passed = v.passed
        s.threshold = threshold
    else:
        s.with_threshold(threshold)
    return s
