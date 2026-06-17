"""Token/cost accounting with graceful degradation (metric #14).

Reported path: if the backend ever emits usage (on a CUSTOM event,
RUN_FINISHED.result, or in final state), read it. Estimated path (default for
the live SSE backend, which emits no usage): tokenize prompt + completion with
tiktoken and flag the result as ESTIMATED. No backend change is required.
"""

from __future__ import annotations

from ...core.run_record import (
    Event,
    ReasoningSegment,
    TokenUsage,
    ToolCall,
    UsageSource,
)

_USAGE_KEYS = ("input_tokens", "output_tokens", "total_tokens",
               "prompt_tokens", "completion_tokens")


def _coerce_usage(obj: object) -> TokenUsage | None:
    if not isinstance(obj, dict):
        return None
    # unwrap common containers
    for container in ("usage", "tokens", "tokenUsage", "token_usage"):
        if isinstance(obj.get(container), dict):
            inner = _coerce_usage(obj[container])
            if inner is not None:
                return inner
    if not any(k in obj for k in _USAGE_KEYS):
        return None
    it = obj.get("input_tokens", obj.get("prompt_tokens"))
    ot = obj.get("output_tokens", obj.get("completion_tokens"))
    tt = obj.get("total_tokens")
    if tt is None and (it is not None or ot is not None):
        tt = (it or 0) + (ot or 0)
    cost = obj.get("cost_usd") or obj.get("cost")
    return TokenUsage(
        source=UsageSource.REPORTED,
        input_tokens=it,
        output_tokens=ot,
        total_tokens=tt,
        cost_usd=cost if isinstance(cost, (int, float)) else None,
    )


def _find_reported_usage(events: list[Event], final_state: dict | None) -> TokenUsage | None:
    for e in events:
        if e.type in ("RUN_FINISHED", "CUSTOM", "RAW"):
            for key in ("result", "value", "payload"):
                u = _coerce_usage(e.payload.get(key))
                if u is not None:
                    return u
            u = _coerce_usage(e.payload)
            if u is not None:
                return u
    if final_state:
        return _coerce_usage(final_state)
    return None


def _encode_len(text: str, encoding: str) -> tuple[int, str]:
    try:
        import tiktoken

        enc = tiktoken.get_encoding(encoding)
        return len(enc.encode(text)), f"tiktoken:{encoding}"
    except Exception:
        # Offline / no encoding available: fall back to a whitespace count.
        return len(text.split()), "wordcount"


def compute_usage(
    *,
    input_messages: list[dict],
    assistant_text: str,
    tool_calls: list[ToolCall],
    reasoning: list[ReasoningSegment],
    events: list[Event],
    final_state: dict | None = None,
    encoding: str = "cl100k_base",
) -> TokenUsage:
    reported = _find_reported_usage(events, final_state)
    if reported is not None:
        return reported

    prompt_text = "\n".join(
        str(m.get("content", "")) for m in input_messages if m.get("content")
    )
    completion_parts = [assistant_text]
    completion_parts += [tc.args_raw or "" for tc in tool_calls]
    completion_parts += [seg.text for seg in reasoning]
    completion_text = "\n".join(p for p in completion_parts if p)

    it, est = _encode_len(prompt_text, encoding)
    ot, _ = _encode_len(completion_text, encoding)
    return TokenUsage(
        source=UsageSource.ESTIMATED,
        input_tokens=it,
        output_tokens=ot,
        total_tokens=it + ot,
        estimator=est,
    )
