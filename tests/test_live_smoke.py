"""Live end-to-end smoke test against a running backend.

SKIPPED unless ``AGENT_EVALS_LIVE_URL`` points at the agent's SSE endpoint
(e.g. ``http://localhost:8080/api/v1/bff/ai/agent/sse``). Auth: a minted local
JWT by default (backend ``local`` profile), or a real bearer token via
``AGENT_EVALS_TOKEN``.

Run it:
    AGENT_EVALS_LIVE_URL=http://localhost:8080/api/v1/bff/ai/agent/sse \
    AGENT_EVALS_USER_LOGIN_ID=TEST0001 pytest tests/test_live_smoke.py -v
"""

from __future__ import annotations

import os

import pytest

from agent_evals.core.run_record import CompletionStatus, UsageSource
from agent_evals.envfile import load_dotenv
from agent_evals.transport import (
    AgUiSseTransport,
    Identity,
    LocalJwtMinter,
    Session,
    StaticTokenProvider,
)

load_dotenv()  # let a local .env supply AGENT_EVALS_* (incl. LIVE_URL to un-skip)
LIVE_URL = os.getenv("AGENT_EVALS_LIVE_URL")
pytestmark = pytest.mark.skipif(not LIVE_URL, reason="set AGENT_EVALS_LIVE_URL to run the live smoke test")


def _resolve_verify():
    """TLS config from env (for corporate / private-CA endpoints):
      AGENT_EVALS_INSECURE=1       -> disable verification (dev only)
      AGENT_EVALS_USE_TRUSTSTORE=1 -> use the OS trust store / macOS Keychain (recommended)
      AGENT_EVALS_CA_BUNDLE=<path> -> verify against a custom CA .pem
    """
    if os.getenv("AGENT_EVALS_INSECURE", "").lower() in ("1", "true", "yes"):
        return False
    if os.getenv("AGENT_EVALS_USE_TRUSTSTORE", "").lower() in ("1", "true", "yes"):
        import truststore  # pip install truststore

        truststore.inject_into_ssl()
    return os.getenv("AGENT_EVALS_CA_BUNDLE") or True


def _session() -> Session:
    user_login_id = os.getenv("AGENT_EVALS_USER_LOGIN_ID", "TEST0001")
    token = os.getenv("AGENT_EVALS_TOKEN")
    if token:
        # a real/pre-made bearer token (e.g. SSO, or paste of the FE dev-token)
        provider = StaticTokenProvider(token)
    else:
        # no-SSO dev access: mint the unsigned token the dev-profile backend trusts.
        # scope is REQUIRED by the chat SSE endpoint; roles drive feature/data gating.
        roles = [r.strip() for r in os.getenv("AGENT_EVALS_ROLES", "GEB_HR,HR_WITH_HR,ADMIN").split(",") if r.strip()]
        scopes = [s.strip() for s in os.getenv("AGENT_EVALS_SCOPE", "readwrite.api.bff").split(",") if s.strip()]
        provider = LocalJwtMinter(user_login_id, roles=roles, scopes=scopes)
    transport = AgUiSseTransport(
        LIVE_URL, persist_dir=os.getenv("AGENT_EVALS_PERSIST"), verify=_resolve_verify()
    )
    return Session(transport, Identity(user_id=user_login_id, token_provider=provider), timeout_s=120)


def test_live_single_turn_well_formed_runrecord():
    rec = _session().ask("Suggest skills I should add to my profile")

    assert rec.completion_status == CompletionStatus.COMPLETED, rec.error
    assert rec.stream_health.run_started_seen and rec.stream_health.run_finished_seen
    # a text answer and/or at least one tool call
    assert rec.assistant_text.strip() or rec.tool_calls
    # client-observed latency is sane
    assert rec.timing.ttft_ms is not None and rec.timing.total_ms is not None
    assert rec.timing.ttft_ms <= rec.timing.total_ms
    # usage available (estimated for SSE)
    assert rec.usage.source in (UsageSource.ESTIMATED, UsageSource.REPORTED)
    # arrival timestamps are monotonic non-decreasing
    arrivals = [e.arrival_ms for e in rec.events if e.arrival_ms is not None]
    assert arrivals == sorted(arrivals)
    # a clean stream has no protocol-invariant breaches
    assert not rec.stream_health.ordering_violations


def test_live_multi_turn_session_accumulates():
    session = _session()
    r1 = session.ask("Suggest skills I should add to my profile")
    n_after = len(session.state.messages)
    r2 = session.ask("Add Python and Docker to my top skills")

    # both turns must actually succeed (don't let an errored run pass silently)
    assert r1.completion_status == CompletionStatus.COMPLETED, r1.error
    assert r2.completion_status == CompletionStatus.COMPLETED, r2.error
    assert r1.turn_index == 0 and r2.turn_index == 1
    assert session.state.thread_id  # stable across turns
    assert len(session.state.messages) > n_after  # history grew
