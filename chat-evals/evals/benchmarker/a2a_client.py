"""
A2A HTTP client for calling an agent endpoint.

Returns a structured v1 response shape so trace-aware scorers can inspect tool
calls, sub-agent attribution, and per-task performance metadata. A
backward-compat helper ``extract_text`` keeps the legacy text-only contract
working for callers that haven't migrated to structured outputs.

Task metadata keys are namespaced (e.g. ``agent.latency_ms``). The namespace
defaults to ``agent`` and can be overridden with the ``A2A_METADATA_NS``
environment variable to match whatever prefix your agent emits.
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import Any

import requests

logger = logging.getLogger("benchmarker.a2a_client")

# Namespace prefix for task-metadata keys the agent emits (e.g. "agent.tokens").
NS = os.environ.get("A2A_METADATA_NS", "agent")


class A2ARequestError(Exception):
    """Raised when an A2A request fails."""


@dataclass
class A2AResponse:
    """Structured v1 response surface.

    ``text`` is always populated (final assistant text). ``trace`` is the parsed
    Trace payload from the ``execution_trace`` artifact, if present.
    ``artifacts`` is a name→data map of all named artifacts (excluding the
    trace). ``metadata`` is the raw task metadata dict (keys prefixed with the
    configured namespace, ``agent.`` by default). ``raw`` is the complete
    JSON-RPC response for callers that need fields not surfaced here.
    """

    text: str
    trace: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    state: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def events(self) -> list[dict[str, Any]]:
        return list(self.trace.get("events", []) or [])

    @property
    def agent_token_totals(self) -> dict[str, dict[str, int]]:
        return dict(self.trace.get("agent_token_totals", {}) or {})

    @property
    def latency_ms(self) -> int | None:
        v = self.metadata.get(f"{NS}.latency_ms")
        return int(v) if v is not None else None

    @property
    def tokens(self) -> dict[str, int]:
        return dict(self.metadata.get(f"{NS}.tokens") or {})

    @property
    def cost_usd(self) -> float | None:
        cost = self.metadata.get(f"{NS}.cost") or {}
        usd = cost.get("usd") if isinstance(cost, dict) else None
        return float(usd) if usd is not None else None

    @property
    def error(self) -> dict[str, Any] | None:
        err = self.metadata.get(f"{NS}.error")
        return dict(err) if isinstance(err, dict) else None


def _get_part_kind(part: dict) -> str:
    return part.get("kind", part.get("type", ""))


def _get_part_data(part: dict) -> Any:
    if _get_part_kind(part) != "data":
        return None
    return part.get("data")


def _get_part_text(part: dict) -> str:
    if _get_part_kind(part) != "text":
        return ""
    return part.get("text", "") or ""


def extract_text(result: dict) -> str:
    """Pull the final agent text out of a Task ``result`` dict.

    Walks ``status.message.parts`` for the first TextPart, then falls back to
    artifact TextParts. Kept for backward compatibility with text-only callers.
    """
    status = result.get("status") or {}
    message = status.get("message") or {}
    for part in (message.get("parts") or []):
        text = _get_part_text(part)
        if text:
            return text
    for artifact in (result.get("artifacts") or []):
        for part in (artifact.get("parts") or []):
            text = _get_part_text(part)
            if text:
                return text
    if isinstance(result, dict) and "text" in result:
        return result["text"]
    return ""


def _parse_response(response_json: dict) -> A2AResponse:
    """Build an A2AResponse from a raw JSON-RPC response."""
    if "error" in response_json and response_json["error"]:
        error = response_json["error"]
        raise A2ARequestError(f"A2A returned error: {error.get('message', error)}")

    result = response_json.get("result") or {}
    text = extract_text(result)
    status = result.get("status") or {}
    state = (status.get("state") or "").replace("-", "_")

    trace: dict[str, Any] = {}
    artifacts_map: dict[str, Any] = {}
    for artifact in (result.get("artifacts") or []):
        name = artifact.get("name") or ""
        for part in (artifact.get("parts") or []):
            data = _get_part_data(part)
            if data is None:
                continue
            if name == "execution_trace" and not trace:
                trace = data if isinstance(data, dict) else {}
            elif name:
                # Stash named artifacts (last-wins for repeats).
                artifacts_map[name] = data

    metadata = result.get("metadata") or {}

    return A2AResponse(
        text=text,
        trace=trace,
        artifacts=artifacts_map,
        metadata=metadata,
        state=state,
        raw=response_json,
    )


def create_graphql_thread(base_url: str, headers: dict) -> str:
    """Create a conversation thread via a GraphQL ``createThread`` mutation.

    Some services require a server-minted thread/conversation id before
    accepting A2A messages. This derives the GraphQL URL from ``base_url`` (by
    replacing the ``/api/...`` suffix with ``/graphql``) and returns the new id.
    """
    parts = base_url.split("/api/")
    graphql_url = parts[0] + "/graphql"

    mutation = """
    mutation CreateThread {
        createThread {
            id
        }
    }
    """

    resp = requests.post(
        graphql_url,
        json={"query": mutation},
        headers={**headers, "Content-Type": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    thread_id = data.get("data", {}).get("createThread", {}).get("id")
    if not thread_id:
        raise A2ARequestError(f"Failed to create thread. GraphQL response: {data}")

    logger.debug("Created thread: %s", thread_id)
    return thread_id


def make_a2a_predict_fn(
    base_url: str,
    headers: dict | None = None,
    *,
    return_structured: bool = True,
):
    """Create a predict function that calls the A2A endpoint.

    Parameters
    ----------
    base_url : str
        The A2A endpoint URL.
    headers : dict | None
        Optional HTTP headers (e.g. Authorization for token-protected endpoints).
    return_structured : bool
        When True (default), the predict function returns an ``A2AResponse``
        dataclass exposing text + trace + artifacts + metadata. When False,
        returns a bare ``str`` for backward compatibility with legacy callers.
    """
    request_headers = {"Content-Type": "application/json"}
    if headers:
        request_headers.update(headers)

    def predict_fn(question: str, context_id: str | None = None, **kwargs):
        message_id = str(uuid.uuid4())

        message = {
            "kind": "message",
            "messageId": message_id,
            "role": "user",
            "parts": [
                {"kind": "text", "text": question}
            ],
        }
        if context_id:
            message["contextId"] = context_id

        payload = {
            "jsonrpc": "2.0",
            "id": message_id,
            "method": "message/send",
            "params": {
                "message": message,
            },
        }

        try:
            resp = requests.post(
                base_url,
                json=payload,
                headers=request_headers,
                timeout=120,
            )
            resp.raise_for_status()
            response_json = resp.json()
        except requests.RequestException as exc:
            raise A2ARequestError(f"A2A request failed: {exc}") from exc

        structured = _parse_response(response_json)
        logger.debug(
            "A2A response: state=%s text=%d-chars events=%d tokens=%s",
            structured.state,
            len(structured.text),
            len(structured.events),
            structured.tokens,
        )

        if return_structured:
            return structured
        return structured.text

    return predict_fn
