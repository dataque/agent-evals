"""Tests for evals.hr_benchmarker.a2a_client — parses hr-agent v1 responses."""

from __future__ import annotations

import json
import pathlib

import pytest


SCHEMA_PATH = (
    pathlib.Path(__file__).resolve().parents[1] / "schemas" / "a2a_response.v1.json"
)


def _v1_response_fixture() -> dict:
    return {
        "jsonrpc": "2.0",
        "id": "req-1",
        "result": {
            "id": "task-1",
            "contextId": "ctx-1",
            "kind": "task",
            "status": {
                "state": "completed",
                "timestamp": "2026-04-28T00:00:00Z",
                "message": {
                    "kind": "message",
                    "messageId": "msg-1",
                    "role": "agent",
                    "parts": [{"kind": "text", "text": "Found 3 matching roles."}],
                    "metadata": {"hr-agent.agent_id": "orchestrator"},
                },
            },
            "history": [],
            "artifacts": [
                {
                    "artifactId": "art-1",
                    "name": "execution_trace",
                    "parts": [
                        {
                            "kind": "data",
                            "data": {
                                "schema": "hr-agent/Trace@v1",
                                "trace_id": "trace-1",
                                "events": [
                                    {
                                        "span_id": "s1",
                                        "parent_span_id": None,
                                        "sequence": 0,
                                        "type": "route",
                                        "agent_id": "orchestrator",
                                        "timestamp": "2026-04-28T00:00:00Z",
                                        "data": {"route_to": "job_discovery", "reason": ""},
                                    },
                                    {
                                        "span_id": "s2",
                                        "parent_span_id": "s1",
                                        "sequence": 1,
                                        "type": "tool_call",
                                        "agent_id": "job_discovery",
                                        "timestamp": "2026-04-28T00:00:01Z",
                                        "data": {
                                            "tool_call_id": "tc1",
                                            "attempt": 0,
                                            "tool_name": "search_jobs",
                                            "args": {"q": "python"},
                                        },
                                    },
                                ],
                                "agent_token_totals": {
                                    "orchestrator": {"input": 50, "output": 12},
                                },
                            },
                        }
                    ],
                    "metadata": {"hr-agent.streamable": True},
                },
                {
                    "artifactId": "art-2",
                    "name": "matched_jobs",
                    "parts": [
                        {
                            "kind": "data",
                            "data": {"schema": "hr-agent/JobCard@v1", "items": [{"id": 1}]},
                        }
                    ],
                },
            ],
            "metadata": {
                "hr-agent.schema_version": "1.0",
                "hr-agent.latency_ms": 1234,
                "hr-agent.tokens": {"input": 50, "output": 12, "total": 62},
                "hr-agent.models": ["gpt-4o"],
                "hr-agent.cost": {
                    "usd": 0.001,
                    "rates_version": "2025-04",
                    "by_model": {"gpt-4o": {"input_per_1k": 0.0025, "output_per_1k": 0.01}},
                },
                "hr-agent.identity_source": "a2a-message-metadata",
            },
        },
    }


class TestParseResponse:
    def test_extracts_text_trace_artifacts_metadata(self):
        from evals.hr_benchmarker.a2a_client import _parse_response

        resp = _parse_response(_v1_response_fixture())
        assert resp.text == "Found 3 matching roles."
        assert resp.state == "completed"
        assert resp.trace["schema"] == "hr-agent/Trace@v1"
        assert len(resp.events) == 2
        assert resp.artifacts["matched_jobs"]["schema"] == "hr-agent/JobCard@v1"
        assert resp.tokens == {"input": 50, "output": 12, "total": 62}
        assert resp.latency_ms == 1234
        assert resp.cost_usd == 0.001
        assert resp.error is None

    def test_failed_response_surfaces_error(self):
        from evals.hr_benchmarker.a2a_client import _parse_response

        fixture = _v1_response_fixture()
        fixture["result"]["status"]["state"] = "failed"
        fixture["result"]["metadata"]["hr-agent.error"] = {
            "code": "tool_error",
            "type": "ConnectionError",
        }
        resp = _parse_response(fixture)
        assert resp.state == "failed"
        assert resp.error == {"code": "tool_error", "type": "ConnectionError"}

    def test_jsonrpc_error_raises(self):
        from evals.hr_benchmarker.a2a_client import _parse_response, A2ARequestError

        with pytest.raises(A2ARequestError):
            _parse_response({"jsonrpc": "2.0", "id": "x", "error": {"message": "bad"}})

    def test_extract_text_works_on_legacy_text_only_response(self):
        from evals.hr_benchmarker.a2a_client import extract_text

        result = {
            "status": {
                "message": {"parts": [{"kind": "text", "text": "hi"}]}
            }
        }
        assert extract_text(result) == "hi"


class TestSchemaFixture:
    def test_v1_response_validates_against_schema(self):
        try:
            import jsonschema
        except ImportError:
            pytest.skip("jsonschema not installed")

        with open(SCHEMA_PATH) as f:
            schema = json.load(f)
        jsonschema.validate(_v1_response_fixture(), schema)
