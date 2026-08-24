"""The one module permitted to write to SharePoint — and only ever to the sandbox site.

``/sites/sandbox`` → ``sandboxlist`` is the only writable target in the tenant. Every other
site, including the misleadingly named ``/sites/Test``, is live and read-only (C2).

This module is exempt from the CI write-guard scan, which makes it the place a reviewer looks
first. Two properties keep that exemption honest:

* The site ID is bound at construction and never taken from a caller, so no argument can
  redirect a write to a live site.
* Every request still passes through :class:`SharePointWriteGuard`, which re-checks the ID.
  The binding and the check are independent; both must agree.
"""

from __future__ import annotations

from typing import Any, Self

import httpx

from app.connectors.sharepoint.client import DEFAULT_TIMEOUT, GRAPH_BASE
from app.connectors.sharepoint.guard import SharePointWriteGuard
from app.core.logging import get_logger

logger = get_logger(__name__)


class SharePointSandboxWriter:
    """Writes list items to the sandbox site, for integration tests and seeding.

    Used by the Phase 0 seeding task (§8.1.1) to mirror the live ``Proposals`` schema into
    ``sandboxlist`` and populate synthetic rows — so the write path can be exercised against
    a real Graph endpoint without touching production.
    """

    def __init__(
        self,
        *,
        access_token: str,
        guard: SharePointWriteGuard,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        if guard.sandbox_site_id is None:
            raise ValueError(
                "SharePointSandboxWriter requires a resolved sandbox site ID. "
                "Resolve it with SharePointReadClient.get_site_id() first."
            )
        self._token = access_token
        self._guard = guard
        #: Bound once. Callers never supply a site, so they cannot redirect a write.
        self._site_id: str = guard.sandbox_site_id
        self._http = http or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
        self._owns_http = http is None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    @property
    def site_id(self) -> str:
        return self._site_id

    async def _write(self, method: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        # Belt and braces: the ID is already bound, and the guard checks it again.
        self._guard.check(method, self._site_id)

        response = await self._http.request(
            method,
            f"{GRAPH_BASE}{path}",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        response.raise_for_status()
        logger.info("sharepoint.sandbox_write", method=method, path=path)
        return response.json() if response.content else {}

    async def create_item(self, list_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        return await self._write(
            "POST", f"/sites/{self._site_id}/lists/{list_id}/items", {"fields": fields}
        )

    async def update_item(
        self, list_id: str, item_id: str, fields: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._write(
            "PATCH", f"/sites/{self._site_id}/lists/{list_id}/items/{item_id}/fields", fields
        )

    async def create_column(self, list_id: str, definition: dict[str, Any]) -> dict[str, Any]:
        """Mirror a column definition read from the live list into the sandbox list."""
        return await self._write(
            "POST", f"/sites/{self._site_id}/lists/{list_id}/columns", definition
        )
