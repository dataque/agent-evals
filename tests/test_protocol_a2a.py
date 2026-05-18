"""Unit tests for the A2A protocol adapter — uses a v1 response fixture.

Ported from chat-evals' ``evals/tests/test_a2a_client_v1.py`` semantics:
exercise ``parse_response`` against the canonical v1 envelope shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_evals.protocols.a2a.client import (
    A2ARequestError,
    extract_text,
    parse_response,
)

FIXTURE = {
    "jsonrpc": "2.0",
    "id": "req-1",
    "result": {
        "id": "task-1",
        "contextId": "ctx-1",
        "kind": "task",
        "status": {
            "state": "completed",
            "message": {
                "role": "agent",
                "parts": [{"kind": "text", "text": "Skills saved to your profile."}],
            },
        },
        "artifacts": [
            {
                "name": "execution_trace",
                "parts": [
                    {
                        "kind": "data",
                        "data": {
                            "schema": "hr-agent/Trace@v1",
                            "events": [
                                {
                                    "type": "tool_call",
                                    "data": {"tool_name": "save_skills", "args": {"version": 3}},
                                },
                                {
                                    "type": "tool_result",
                                    "data": {"tool_name": "save_skills", "status": "ok"},
                                },
                            ],
                        },
                    }
                ],
            },
            {
                "name": "matched_jobs",
                "parts": [
                    {
                        "kind": "data",
                        "data": {"schema": "hr-agent/JobCard@v1", "items": []},
                    }
                ],
            },
        ],
        "metadata": {
            "hr-agent.schema_version": "1.0",
            "hr-agent.latency_ms": 1234,
            "hr-agent.tokens": {"input": 500, "output": 50, "total": 550},
        },
    },
}


def test_parse_response_extracts_text():
    r = parse_response(FIXTURE)
    assert r.text == "Skills saved to your profile."


def test_parse_response_extracts_trace():
    r = parse_response(FIXTURE)
    assert r.trace.get("schema") == "hr-agent/Trace@v1"
    assert len(r.events) == 2


def test_parse_response_extracts_artifacts():
    r = parse_response(FIXTURE)
    assert "matched_jobs" in r.artifacts
    assert "execution_trace" not in r.artifacts  # hoisted into .trace


def test_parse_response_extracts_metadata():
    r = parse_response(FIXTURE)
    assert r.metadata["hr-agent.schema_version"] == "1.0"
    assert r.latency_ms == 1234
    assert r.tokens == {"input": 500, "output": 50, "total": 550}


def test_parse_response_state():
    r = parse_response(FIXTURE)
    assert r.state == "completed"


def test_parse_response_state_input_required_dash_to_underscore():
    payload = {
        "jsonrpc": "2.0",
        "id": "req-2",
        "result": {"id": "t", "status": {"state": "input-required"}},
    }
    r = parse_response(payload)
    assert r.state == "input_required"


def test_parse_response_raises_on_error_envelope():
    err = {"jsonrpc": "2.0", "id": "x", "error": {"code": 500, "message": "boom"}}
    with pytest.raises(A2ARequestError):
        parse_response(err)


def test_extract_text_falls_back_to_artifact():
    result = {
        "status": {},
        "artifacts": [{"parts": [{"kind": "text", "text": "fallback"}]}],
    }
    assert extract_text(result) == "fallback"


def test_extract_text_empty_when_nothing():
    assert extract_text({}) == ""


def test_schema_fixture_loads():
    schema_path = (
        Path(__file__).resolve().parent.parent
        / "agent_evals"
        / "protocols"
        / "a2a"
        / "schemas"
        / "v1.json"
    )
    assert schema_path.exists()
    schema = json.loads(schema_path.read_text())
    assert schema.get("title") == "hr-agent A2A response v1"
