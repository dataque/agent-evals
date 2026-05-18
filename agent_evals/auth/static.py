"""Static-token auth providers — bearer token, function key."""

from __future__ import annotations

from .base import AuthProvider


class BearerAuth(AuthProvider):
    """Static Bearer-token auth (chat-evals' ``bff-dev`` target style, raw SSO)."""

    def __init__(self, token: str):
        if not token:
            raise ValueError("BearerAuth token cannot be empty.")
        self._token = token

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}


class FunctionKeyAuth(AuthProvider):
    """Azure Function key passed as a header (alternative to ``?code=...`` in URL)."""

    def __init__(self, key: str, header_name: str = "x-functions-key"):
        if not key:
            raise ValueError("FunctionKeyAuth key cannot be empty.")
        self._key = key
        self._header = header_name

    def headers(self) -> dict[str, str]:
        return {self._header: self._key}
