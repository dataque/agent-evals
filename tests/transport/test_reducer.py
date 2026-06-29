"""Offline regression backbone for the AG-UI reducer.

Feeds canned event lists (no backend) and asserts the resulting RunRecord parts.
Covers every edge case the SSE adapter must survive.
"""

from __future__ import annotations

from agent_evals.core.run_record import CompletionStatus, ToolStatus
from agent_evals.transport.agui.events import parse_event
from agent_evals.transport.agui.reducer import reduce_events
from agent_evals.transport.agui.timing import derive_timing


def ev(t: str, seq: int, ms: float, **payload):
    return parse_event({"type": t, **payload}, seq=seq, arrival_ms=ms, arrival_wall=1000.0 + ms)


def happy_events():
    return [
        ev("RUN_STARTED", 0, 1.0),
        ev("TEXT_MESSAGE_START", 1, 2.0, messageId="m1", role="assistant"),
        ev("TEXT_MESSAGE_CONTENT", 2, 3.0, messageId="m1", delta="Here are "),
        ev("TEXT_MESSAGE_CONTENT", 3, 4.0, messageId="m1", delta="your skills."),
        ev("TEXT_MESSAGE_END", 4, 5.0, messageId="m1"),
        ev("TOOL_CALL_START", 5, 6.0, toolCallId="tc1", toolCallName="suggest_skills", parentMessageId="m1"),
        ev("TOOL_CALL_ARGS", 6, 7.0, toolCallId="tc1", delta='{"max":'),
        ev("TOOL_CALL_ARGS", 7, 8.0, toolCallId="tc1", delta="5}"),
        ev("TOOL_CALL_END", 8, 9.0, toolCallId="tc1"),
        ev("TOOL_CALL_RESULT", 9, 10.0, toolCallId="tc1", messageId="r1", role="tool",
           content='{"top":[{"name":"Python"}],"additional":[]}'),
        ev("RUN_FINISHED", 10, 11.0, threadId="t", runId="r"),
    ]


def test_happy_path():
    r = reduce_events(happy_events())
    assert r.assistant_text == "Here are your skills."
    assert len(r.tool_calls) == 1
    tc = r.tool_calls[0]
    assert tc.name == "suggest_skills"
    assert tc.args == {"max": 5}            # multi-fragment args assembled + parsed
    assert tc.result == {"top": [{"name": "Python"}], "additional": []}
    assert tc.status == ToolStatus.OK
    assert tc.parent_message_id == "m1"
    assert r.completion_status == CompletionStatus.COMPLETED
    assert r.stream_health.is_clean
    # a tool message was emitted
    assert any(m.role == "tool" and m.tool_call_id == "tc1" for m in r.messages)


def test_timing_ttft_before_total():
    events = happy_events()
    r = reduce_events(events)
    t = derive_timing(events, r.tool_calls, aborted=False)
    assert t.ttft_ms == 3.0          # first TEXT_MESSAGE_CONTENT
    assert t.total_ms == 11.0        # RUN_FINISHED
    assert t.ttft_ms < t.total_ms
    assert t.tool_latencies_ms["tc1"] == 4.0  # 10 - 6
    assert t.request_to_run_started_ms == 1.0


def test_concurrent_tool_calls():
    events = [
        ev("RUN_STARTED", 0, 1.0),
        ev("TOOL_CALL_START", 1, 2.0, toolCallId="a", toolCallName="get_skills"),
        ev("TOOL_CALL_START", 2, 3.0, toolCallId="b", toolCallName="get_talent_profile"),
        ev("TOOL_CALL_ARGS", 3, 4.0, toolCallId="b", delta="{}"),
        ev("TOOL_CALL_ARGS", 4, 5.0, toolCallId="a", delta="{}"),
        ev("TOOL_CALL_END", 5, 6.0, toolCallId="a"),
        ev("TOOL_CALL_END", 6, 7.0, toolCallId="b"),
        ev("TOOL_CALL_RESULT", 7, 8.0, toolCallId="a", content="{}"),
        ev("TOOL_CALL_RESULT", 8, 9.0, toolCallId="b", content="{}"),
        ev("RUN_FINISHED", 9, 10.0),
    ]
    r = reduce_events(events)
    assert [tc.name for tc in r.tool_calls] == ["get_skills", "get_talent_profile"]  # by start order
    assert all(tc.status == ToolStatus.OK for tc in r.tool_calls)


def test_missing_end_is_incomplete():
    events = [
        ev("RUN_STARTED", 0, 1.0),
        ev("TOOL_CALL_START", 1, 2.0, toolCallId="x", toolCallName="get_skills"),
        ev("TOOL_CALL_ARGS", 2, 3.0, toolCallId="x", delta="{}"),
        ev("RUN_FINISHED", 3, 4.0),
    ]
    r = reduce_events(events)
    assert r.tool_calls[0].status == ToolStatus.INCOMPLETE
    assert "x" in r.stream_health.unmatched_tool_starts
    assert not r.stream_health.is_clean


def test_bad_args_json():
    events = [
        ev("RUN_STARTED", 0, 1.0),
        ev("TOOL_CALL_START", 1, 2.0, toolCallId="x", toolCallName="save_skills"),
        ev("TOOL_CALL_ARGS", 2, 3.0, toolCallId="x", delta="{not json"),
        ev("TOOL_CALL_END", 3, 4.0, toolCallId="x"),
        ev("TOOL_CALL_RESULT", 4, 5.0, toolCallId="x", content="{}"),
        ev("RUN_FINISHED", 5, 6.0),
    ]
    r = reduce_events(events)
    assert r.tool_calls[0].status == ToolStatus.BAD_ARGS
    assert r.tool_calls[0].args is None
    assert "x" in r.stream_health.malformed_arg_tool_calls


def test_orphan_result():
    events = [
        ev("RUN_STARTED", 0, 1.0),
        ev("TOOL_CALL_RESULT", 1, 2.0, toolCallId="ghost", content="{}"),
        ev("RUN_FINISHED", 2, 3.0),
    ]
    r = reduce_events(events)
    assert r.tool_calls[0].status == ToolStatus.ORPHAN_RESULT
    assert "ghost" in r.stream_health.orphan_tool_results


def test_run_error():
    events = [
        ev("RUN_STARTED", 0, 1.0),
        ev("RUN_ERROR", 1, 2.0, message="boom", code="E500"),
        ev("RUN_FINISHED", 2, 3.0),
    ]
    r = reduce_events(events)
    assert r.completion_status == CompletionStatus.ERRORED
    assert r.error and r.error.message == "boom" and r.error.code == "E500"


def test_truncated_stream():
    events = [ev("RUN_STARTED", 0, 1.0), ev("TEXT_MESSAGE_CHUNK", 1, 2.0, messageId="m", delta="hi")]
    r = reduce_events(events)
    assert r.completion_status == CompletionStatus.TRUNCATED
    assert r.stream_health.ended_before_finished
    assert r.assistant_text == "hi"   # chunk-mode text still assembled


def test_aborted_timeout_flag():
    r = reduce_events([ev("RUN_STARTED", 0, 1.0)], aborted_timeout=True)
    assert r.completion_status == CompletionStatus.ABORTED_TIMEOUT


def test_state_snapshot_then_delta():
    events = [
        ev("RUN_STARTED", 0, 1.0),
        ev("STATE_SNAPSHOT", 1, 2.0, snapshot={"skills": ["a"], "count": 1}),
        ev("STATE_DELTA", 2, 3.0, delta=[{"op": "add", "path": "/skills/-", "value": "b"},
                                         {"op": "replace", "path": "/count", "value": 2}]),
        ev("RUN_FINISHED", 3, 4.0),
    ]
    r = reduce_events(events)
    assert r.final_state == {"skills": ["a", "b"], "count": 2}
    assert not r.stream_health.state_patch_errors


def test_unknown_event_and_duplicate_run_started():
    events = [
        ev("RUN_STARTED", 0, 1.0),
        ev("RUN_STARTED", 1, 2.0),
        ev("FANCY_NEW_EVENT", 2, 3.0, foo="bar"),
        ev("RUN_FINISHED", 3, 4.0),
    ]
    r = reduce_events(events)
    assert "FANCY_NEW_EVENT" in r.stream_health.unknown_event_types
    assert r.stream_health.duplicate_run_started


def test_string_error_result_flags_error():
    # A backend deserialization error comes back as a bare STRING (not {error:...}).
    # The mutating call must still be ERROR, or audit/action (#16) false-passes a
    # failed write — the real save_skills defect the eval surfaced.
    events = [
        ev("RUN_STARTED", 0, 1.0),
        ev("TOOL_CALL_START", 1, 2.0, toolCallId="s", toolCallName="save_skills"),
        ev("TOOL_CALL_ARGS", 2, 3.0, toolCallId="s", delta='{"top":["Java"]}'),
        ev("TOOL_CALL_END", 3, 4.0, toolCallId="s"),
        ev("TOOL_CALL_RESULT", 4, 5.0, toolCallId="s",
           content="Cannot construct instance of SaveSkillsItemInput: not of type 'object'"),
        ev("RUN_FINISHED", 5, 6.0),
    ]
    r = reduce_events(events)
    tc = r.tool_calls[0]
    assert tc.is_error and tc.status == ToolStatus.ERROR
    # a clean string result is NOT mistaken for an error
    ok = reduce_events([
        ev("RUN_STARTED", 0, 1.0),
        ev("TOOL_CALL_START", 1, 2.0, toolCallId="g", toolCallName="get_skills"),
        ev("TOOL_CALL_END", 2, 3.0, toolCallId="g"),
        ev("TOOL_CALL_RESULT", 3, 4.0, toolCallId="g", content="Here are your skills: Java, React"),
        ev("RUN_FINISHED", 4, 5.0),
    ])
    assert not ok.tool_calls[0].is_error


def test_task_routing_and_steps():
    events = [
        ev("RUN_STARTED", 0, 1.0),
        ev("STEP_STARTED", 1, 2.0, stepName="orchestrator"),
        ev("TOOL_CALL_START", 2, 3.0, toolCallId="t", toolCallName="Task"),
        ev("TOOL_CALL_ARGS", 3, 4.0, toolCallId="t", delta='{"subagent":"requisition-matching-agent"}'),
        ev("TOOL_CALL_END", 4, 5.0, toolCallId="t"),
        ev("TOOL_CALL_START", 5, 6.0, toolCallId="s", toolCallName="suggest_requisitions"),
        ev("TOOL_CALL_ARGS", 6, 7.0, toolCallId="s", delta="{}"),
        ev("TOOL_CALL_END", 7, 8.0, toolCallId="s"),
        ev("TOOL_CALL_RESULT", 8, 9.0, toolCallId="s", content='{"matches":[]}'),
        ev("STEP_FINISHED", 9, 10.0, stepName="orchestrator"),
        ev("RUN_FINISHED", 10, 11.0),
    ]
    r = reduce_events(events)
    subs = {route.subagent for route in r.subagent_routes}
    assert "requisition-matching-agent" in subs   # synthesized from Task args
    assert "orchestrator" in subs                  # synthesized from step name
    # the requisition tool ran while routed to the subagent
    sug = next(tc for tc in r.tool_calls if tc.name == "suggest_requisitions")
    assert sug.owning_subagent == "requisition-matching-agent"
    assert sug.owning_step_index == 0
    # the step span closed
    assert r.steps[0].ended_arrival_ms == 10.0
