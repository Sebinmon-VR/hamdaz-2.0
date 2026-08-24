"""App-only Microsoft Graph tokens (client credentials).

The counterpart to :mod:`app.core.oidc`: that module answers *which human is this*, this one
authenticates the application itself. The SharePoint sync runs on a schedule with no user in
the request path, so there is no delegated token to borrow — a browser session's token
belongs to that session and expires with it.

Lives in ``core`` rather than in the connector package on purpose. Acquiring a token is a
POST, and ``app/connectors/sharepoint/`` is kept provably free of write verbs by
``scripts/check_sharepoint_readonly.py`` (guard #3, constraint C2). This POST goes to
login.microsoftonline.com and never touches SharePoint, but exempting a file inside that
package would blunt a guard that is worth keeping absolute.

**Read scope only.** The tenant grants this app registration ``Sites.Read.All``; C2 stays
enforced structurally by :class:`~app.connectors.sharepoint.client.SharePointReadClient`
having no write methods, and at runtime by the guard.
"""

from __future__ import annotations

import asyncio
from typing import Any, Final

import httpx

from app.core.config import Settings
from app.core.errors import AppError
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Client credentials cannot request granular scopes — ``.default`` means "every application
#: permission already consented for this app registration in this tenant".
GRAPH_DEFAULT_SCOPE: Final = "https://graph.microsoft.com/.default"
#: Refresh this many seconds before the token actually expires, so a token cannot lapse
#: mid-sync between the check and the Graph call that uses it.
EXPIRY_MARGIN_SECONDS: Final = 120


class GraphAuthError(AppError):
    """The app registration could not obtain a Graph token."""

    status_code = 502
    title = "SharePoint sign-in failed"
    error_code = "graph_auth_failed"


class GraphTokenProvider:
    """Caches one app-only access token and refreshes it just before expiry.

    Deliberately per-process rather than module-global: the legacy connector cached its token
    in a module global that every gunicorn worker mutated independently, and a stale entry
    there took the whole sync down until a restart. One provider instance, one lock.
    """

    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0))
        self._owns_http = http is None
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    @property
    def _token_endpoint(self) -> str:
        return (
            f"https://login.microsoftonline.com/"
            f"{self._settings.azure_tenant_id}/oauth2/v2.0/token"
        )

    def _is_configured(self) -> bool:
        return bool(
            self._settings.azure_tenant_id
            and self._settings.azure_client_id
            and self._settings.azure_client_secret
        )

    async def get_token(self) -> str:
        """A valid app-only Graph token, acquiring or refreshing as needed."""
        if not self._is_configured():
            raise GraphAuthError(
                "Azure app registration is not configured: set AZURE_TENANT_ID, "
                "AZURE_CLIENT_ID and AZURE_CLIENT_SECRET."
            )

        loop = asyncio.get_running_loop()
        if self._token is not None and loop.time() < self._expires_at:
            return self._token

        async with self._lock:
            # Another coroutine may have refreshed while we waited for the lock.
            if self._token is not None and loop.time() < self._expires_at:
                return self._token
            return await self._acquire(loop)

    async def _acquire(self, loop: asyncio.AbstractEventLoop) -> str:
        response = await self._http.post(
            self._token_endpoint,
            data={
                "client_id": self._settings.azure_client_id,
                "client_secret": self._settings.azure_client_secret,
                "grant_type": "client_credentials",
                "scope": GRAPH_DEFAULT_SCOPE,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        if response.status_code != 200:
            detail = _error_detail(response)
            logger.warning(
                "graph.client_credentials_failed",
                status=response.status_code,
                detail=detail,
            )
            raise GraphAuthError(f"Microsoft rejected the app-only token request: {detail}")

        payload: dict[str, Any] = response.json()
        token = payload.get("access_token")
        if not token:
            raise GraphAuthError("Microsoft returned no access_token for the app registration.")

        expires_in = int(payload.get("expires_in", 3600))
        self._token = str(token)
        self._expires_at = loop.time() + max(expires_in - EXPIRY_MARGIN_SECONDS, 30)
        logger.info("graph.token_acquired", expires_in=expires_in)
        return self._token


def _error_detail(response: httpx.Response) -> str:
    """AADSTS codes are the only part of Microsoft's error body worth surfacing.

    ``AADSTS7000215`` is a wrong client secret; ``AADSTS900023`` a wrong tenant. A missing
    admin consent shows up later, as a 403 from Graph itself, not here.
    """
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    code = body.get("error", "unknown_error")
    description = str(body.get("error_description", "")).splitlines()[:1]
    return f"{code}: {description[0]}" if description else str(code)
