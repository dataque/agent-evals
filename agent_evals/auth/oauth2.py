"""OAuth2 auth — Microsoft Entra ID (Azure AD) JWT acquisition via MSAL.

Used by the ``backend`` project plug-in. The token is acquired lazily on first
``headers()`` call and cached in-memory (MSAL also supports persistent caches;
opt in by setting ``ENTRA_TOKEN_CACHE_PATH``).
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from .base import AuthProvider

logger = logging.getLogger("agent_evals.auth.oauth2")

try:
    import msal

    _MSAL_AVAILABLE = True
except ImportError:  # pragma: no cover
    _MSAL_AVAILABLE = False


class EntraIdAuth(AuthProvider):
    """Microsoft Entra ID (formerly Azure AD) JWT — client-credentials flow.

    Parameters
    ----------
    tenant_id
        Entra ID tenant id (or ``ENTRA_TENANT_ID``).
    client_id
        App registration client id (or ``ENTRA_CLIENT_ID``).
    client_secret
        App registration client secret (or ``ENTRA_CLIENT_SECRET``).
    scope
        Resource scope, e.g. ``api://<api-app-id>/.default`` (or ``ENTRA_SCOPE``).
    token_cache_path
        Optional path to a persistent token cache file (or
        ``ENTRA_TOKEN_CACHE_PATH``). Defaults to in-memory.
    refresh_buffer
        Seconds before expiry to proactively refresh. Default 300s.
    """

    def __init__(
        self,
        tenant_id: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        scope: str | None = None,
        token_cache_path: str | None = None,
        refresh_buffer: int = 300,
    ):
        if not _MSAL_AVAILABLE:
            raise ImportError(
                "msal is required for EntraIdAuth. `pip install msal>=1.26`."
            )
        self.tenant_id = tenant_id or os.environ.get("ENTRA_TENANT_ID", "")
        self.client_id = client_id or os.environ.get("ENTRA_CLIENT_ID", "")
        self.client_secret = client_secret or os.environ.get("ENTRA_CLIENT_SECRET", "")
        self.scope = scope or os.environ.get("ENTRA_SCOPE", "")
        self.token_cache_path = token_cache_path or os.environ.get(
            "ENTRA_TOKEN_CACHE_PATH"
        )
        self.refresh_buffer = refresh_buffer

        missing = [
            n for n, v in (
                ("ENTRA_TENANT_ID", self.tenant_id),
                ("ENTRA_CLIENT_ID", self.client_id),
                ("ENTRA_CLIENT_SECRET", self.client_secret),
                ("ENTRA_SCOPE", self.scope),
            ) if not v
        ]
        if missing:
            raise ValueError(
                f"EntraIdAuth missing required config: {', '.join(missing)}. "
                f"Set the corresponding env vars or pass constructor args."
            )

        self._token: str | None = None
        self._expires_at: float = 0.0
        self._app: msal.ConfidentialClientApplication | None = None

    def _ensure_app(self) -> msal.ConfidentialClientApplication:
        if self._app is not None:
            return self._app

        cache: msal.SerializableTokenCache | None = None
        if self.token_cache_path:
            cache = msal.SerializableTokenCache()
            cache_file = Path(self.token_cache_path)
            if cache_file.exists():
                cache.deserialize(cache_file.read_text())

        self._app = msal.ConfidentialClientApplication(
            client_id=self.client_id,
            client_credential=self.client_secret,
            authority=f"https://login.microsoftonline.com/{self.tenant_id}",
            token_cache=cache,
        )
        return self._app

    def _persist_cache(self) -> None:
        if self.token_cache_path and self._app is not None:
            cache = self._app.token_cache
            if isinstance(cache, msal.SerializableTokenCache) and cache.has_state_changed:
                Path(self.token_cache_path).write_text(cache.serialize())

    def _acquire_token(self) -> str:
        app = self._ensure_app()
        result = app.acquire_token_for_client(scopes=[self.scope])
        if "access_token" not in result:
            raise RuntimeError(
                f"EntraID token acquisition failed: "
                f"{result.get('error_description') or result.get('error') or result}"
            )
        self._persist_cache()
        self._token = result["access_token"]
        self._expires_at = time.time() + int(result.get("expires_in", 3600))
        logger.info("Acquired Entra ID token, expires in %ds", result.get("expires_in", 3600))
        return self._token

    def headers(self) -> dict[str, str]:
        if self._token is None or time.time() > (self._expires_at - self.refresh_buffer):
            self._acquire_token()
        return {"Authorization": f"Bearer {self._token}"}
