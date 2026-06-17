"""AG-UI event taxonomy and a typed parser.

We hand-roll the parser (rather than depend on an AG-UI SDK) for three reasons:
client-observed arrival timestamps must be stamped as events come off the
socket; the backend has wire quirks (a leading-space ``data:`` prefix); and the
parse surface is small. Event field names mirror the AG-UI protocol exactly.
"""

from __future__ import annotations

from ...core.run_record import Event


class ET:
    """AG-UI event type string constants (verbatim wire values)."""

    RUN_STARTED = "RUN_STARTED"
    RUN_FINISHED = "RUN_FINISHED"
    RUN_ERROR = "RUN_ERROR"

    TEXT_MESSAGE_START = "TEXT_MESSAGE_START"
    TEXT_MESSAGE_CONTENT = "TEXT_MESSAGE_CONTENT"
    TEXT_MESSAGE_END = "TEXT_MESSAGE_END"
    TEXT_MESSAGE_CHUNK = "TEXT_MESSAGE_CHUNK"

    TOOL_CALL_START = "TOOL_CALL_START"
    TOOL_CALL_ARGS = "TOOL_CALL_ARGS"
    TOOL_CALL_END = "TOOL_CALL_END"
    TOOL_CALL_CHUNK = "TOOL_CALL_CHUNK"
    TOOL_CALL_RESULT = "TOOL_CALL_RESULT"

    REASONING_START = "REASONING_START"
    REASONING_MESSAGE_START = "REASONING_MESSAGE_START"
    REASONING_MESSAGE_CONTENT = "REASONING_MESSAGE_CONTENT"
    REASONING_MESSAGE_END = "REASONING_MESSAGE_END"
    REASONING_END = "REASONING_END"

    THINKING_START = "THINKING_START"
    THINKING_END = "THINKING_END"
    THINKING_TEXT_MESSAGE_START = "THINKING_TEXT_MESSAGE_START"
    THINKING_TEXT_MESSAGE_CONTENT = "THINKING_TEXT_MESSAGE_CONTENT"
    THINKING_TEXT_MESSAGE_END = "THINKING_TEXT_MESSAGE_END"

    STEP_STARTED = "STEP_STARTED"
    STEP_FINISHED = "STEP_FINISHED"

    STATE_SNAPSHOT = "STATE_SNAPSHOT"
    STATE_DELTA = "STATE_DELTA"
    MESSAGES_SNAPSHOT = "MESSAGES_SNAPSHOT"

    RAW = "RAW"
    CUSTOM = "CUSTOM"


KNOWN_EVENT_TYPES: set[str] = {
    v for k, v in vars(ET).items() if not k.startswith("_") and isinstance(v, str)
}

# Events that represent the first user-visible token of progress (for TTFT).
# Deliberately excludes RUN_STARTED / STEP_STARTED control events.
CONTENT_BEARING: set[str] = {
    ET.TEXT_MESSAGE_CONTENT,
    ET.TEXT_MESSAGE_CHUNK,
    ET.TOOL_CALL_START,
    ET.TOOL_CALL_CHUNK,
    ET.THINKING_TEXT_MESSAGE_CONTENT,
    ET.REASONING_MESSAGE_CONTENT,
}


def parse_event(raw: dict, *, seq: int, arrival_ms: float, arrival_wall: float) -> Event:
    """Wrap a decoded AG-UI event object in a timestamped :class:`Event`.

    The whole decoded object is kept as both ``payload`` (read by the reducer)
    and ``raw`` (for the artifact log); unknown types pass through verbatim.
    """
    etype = raw.get("type") or "UNKNOWN"
    server_ts = raw.get("timestamp")
    return Event(
        seq=seq,
        type=etype,
        arrival_ms=arrival_ms,
        arrival_wall=arrival_wall,
        server_timestamp=server_ts if isinstance(server_ts, int) else None,
        payload=raw,
        raw=raw,
    )
