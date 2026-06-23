"""Auth token providers for driving the system-under-test.

``LocalJwtMinter`` targets the backend's ``local`` Spring profile, which parses
the JWT payload WITHOUT signature/issuer/expiry verification — so an unsigned
token with the right user claim is sufficient for local eval runs. Real
environments plug in a static token or a refresh callable.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable
from typing import Protocol, runtime_checkable

# The standard JWT subject claim. A backend reads the caller's id from a claim
# whose NAME is deployment-specific, so it is configurable per target (set
# ``user_claim`` on the target / ``LocalJwtMinter``); this generic default keeps
# the harness usable out of the box against any backend that honors ``sub``.
DEFAULT_USER_CLAIM = "sub"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


@runtime_checkable
class TokenProvider(Protocol):
    def get_token(self) -> str:
        """Return the bearer token value (without the leading ``Bearer ``)."""
        ...


class LocalJwtMinter:
    """Mint an unsigned (``alg=none``) JWT that a backend running under its
    no-auth dev profile self-trusts (it parses the token without verifying it).

    The token shape mirrors the frontend's dev-token generator so the same
    identity contract holds: ``user_claim`` → userId, ``roles`` → ``ROLE_*``
    authorities, and the space-delimited ``scope`` → ``SCOPE_*`` authorities.
    NOTE: protected endpoints (e.g. the chat SSE endpoint) typically require a
    specific scope — pass it via ``scopes`` (configured per target), otherwise
    the call is authenticated but not authorized.
    """

    def __init__(
        self,
        user_login_id: str,
        *,
        roles: list[str] | None = None,
        scopes: list[str] | None = None,
        user_claim: str | None = None,
        subject: str | None = None,
        ttl_seconds: int = 365 * 24 * 60 * 60,
        extra_claims: dict | None = None,
    ) -> None:
        self.user_login_id = user_login_id
        self.roles = list(roles or [])
        self.scopes = list(scopes or [])
        self.user_claim = user_claim or DEFAULT_USER_CLAIM
        self.subject = subject or user_login_id
        self.ttl_seconds = ttl_seconds
        self.extra_claims = dict(extra_claims or {})

    def get_token(self) -> str:
        header = _b64url(json.dumps({"alg": "none", "typ": "JWT"}).encode())
        now = int(time.time())
        payload = {
            self.user_claim: self.user_login_id,  # userId claim the backend reads
            "sub": self.subject,
            "roles": self.roles,                 # -> ROLE_* authorities
            "scope": " ".join(self.scopes),      # space-delimited -> SCOPE_* authorities
            "iat": now,
            "nbf": now,
            "exp": now + self.ttl_seconds,
            **self.extra_claims,
        }
        body = _b64url(json.dumps(payload).encode())
        return f"{header}.{body}.dev"  # 3 segments; the signature is ignored


class StaticTokenProvider:
    """Use a fixed token (e.g. an SSO access token pasted for a dev/uat run)."""

    def __init__(self, token: str) -> None:
        self._token = token.removeprefix("Bearer ").strip()

    def get_token(self) -> str:
        return self._token


class CallableTokenProvider:
    """Fetch the token on demand (e.g. an OIDC client-credentials refresh)."""

    def __init__(self, fn: Callable[[], str]) -> None:
        self._fn = fn

    def get_token(self) -> str:
        return self._fn().removeprefix("Bearer ").strip()
