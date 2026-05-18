"""A2A protocol adapter — wraps the A2A client in the ProtocolAdapter interface.

The adapter also handles thread provisioning: for endpoints that require a
server-side thread (backend BFF, chat-evals' bff-dev target), it creates one
via GraphQL; for endpoints that accept a client-generated ``contextId``
(chat-evals' fa target), it returns a fresh UUID.
"""

from __future__ import annotations

from typing import Any

from agent_evals.auth import AuthProvider, NoAuth
from agent_evals.core.protocol import PredictRequest, PredictResponse, ProtocolAdapter
from agent_evals.core.trace import Trace

from .client import (
    A2ARequestError,
    A2AResponse,
    create_bff_thread,
    make_a2a_predict_fn,
)


class A2AAdapter(ProtocolAdapter):
    """A2A JSON-RPC protocol adapter.

    Parameters
    ----------
    base_url
        Full URL to the A2A endpoint (`message/send` is appended via the
        JSON-RPC method field, not the URL).
    auth
        ``AuthProvider`` that injects request headers. Defaults to ``NoAuth``.
    server_side_threads
        When True, calls ``create_bff_thread`` against the matching GraphQL
        endpoint to provision a thread id. When False, returns a UUID4 in
        ``new_thread_id``.
    timeout
        HTTP timeout in seconds.
    """

    def __init__(
        self,
        base_url: str,
        auth: AuthProvider | None = None,
        *,
        server_side_threads: bool = False,
        timeout: int = 120,
    ):
        self.base_url = base_url
        self.auth = auth or NoAuth()
        self.server_side_threads = server_side_threads
        self.timeout = timeout
        self._predict = make_a2a_predict_fn(
            base_url=base_url,
            headers=self.auth.headers(),
            return_structured=True,
            timeout=timeout,
        )

    def send(self, request: PredictRequest, **kwargs: Any) -> PredictResponse:
        try:
            a2a_resp: A2AResponse = self._predict(
                request.question,
                context_id=request.thread_id,
                **kwargs,
            )
        except A2ARequestError:
            raise
        return PredictResponse(
            text=a2a_resp.text,
            trace=Trace.from_dict(a2a_resp.trace),
            artifacts=dict(a2a_resp.artifacts),
            metadata=dict(a2a_resp.metadata),
            state=a2a_resp.state,
            raw=a2a_resp.raw,
        )

    def new_thread_id(self) -> str:
        if self.server_side_threads:
            return create_bff_thread(self.base_url, self.auth.headers())
        return super().new_thread_id()
