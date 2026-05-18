"""Auth provider base class."""

from __future__ import annotations

from abc import ABC, abstractmethod


class AuthProvider(ABC):
    """Produces HTTP request headers for the protocol adapter."""

    @abstractmethod
    def headers(self) -> dict[str, str]:
        """Return the headers dict to merge into outgoing requests."""


class NoAuth(AuthProvider):
    """No-op auth (function-key-in-URL or anonymous endpoints)."""

    def headers(self) -> dict[str, str]:
        return {}
