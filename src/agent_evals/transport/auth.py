"""Auth token providers for driving the system-under-test.

``LocalJwtMinter`` targets the backend's ``local`` Spring profile, which parses
the JWT payload WITHOUT signature/issuer/expiry verification — so an unsigned
token with the right user claim is sufficient for local eval runs. Real
environments plug in a static token or a refresh callable.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from typing import Protocol, runtime_checkable

# The backend reads the caller's id from this JWT claim. It is configurable so
# the harness stays generic across deployments.
DEFAULT_USER_CLAIM = "ubs_auth_gpn"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


@runtime_checkable
class TokenProvider(Protocol):
    def get_token(self) -> str:
        """Return the bearer token value (without the leading ``Bearer ``)."""
        ...


class LocalJwtMinter:
    """Mint an unsigned JWT carrying ``user_claim=gpn`` for local-profile runs."""

    def __init__(
        self,
        gpn: str,
        *,
        roles: list[str] | None = None,
        scopes: list[str] | None = None,
        user_claim: str = DEFAULT_USER_CLAIM,
        extra_claims: dict | None = None,
    ) -> None:
        self.gpn = gpn
        self.roles = list(roles or [])
        self.scopes = list(scopes or [])
        self.user_claim = user_claim
        self.extra_claims = dict(extra_claims or {})

    def get_token(self) -> str:
        header = _b64url(json.dumps({"alg": "none", "typ": "JWT"}).encode())
        payload = {
            self.user_claim: self.gpn,
            "roles": self.roles,
            "scope": " ".join(self.scopes),
            **self.extra_claims,
        }
        body = _b64url(json.dumps(payload).encode())
        return f"{header}.{body}.sig"  # 3 segments; signature is ignored locally


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
