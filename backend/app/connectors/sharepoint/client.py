"""Read-only SharePoint connector — guard #1, the structural one.

This class has **no write methods**. Not disabled ones, not ones behind a flag: there is no
``create``, ``update``, ``patch``, ``upload`` or ``delete`` to call, from a service, a worker
or a developer-panel tool. You cannot misuse an API that does not exist.

Write capability lives in :mod:`app.connectors.sharepoint.sandbox`, which is the only module
in this package permitted to name a write verb — and the only one the CI grep exempts.

If you are here to add a write, stop and read ``docs/PROJECT_PLAN.md`` §8.1 first.
"""

from __future__ import annotations

from typing import Any, Final, Self

import httpx

from app.connectors.sharepoint.guard import SharePointWriteGuard
from app.core.logging import get_logger

logger = get_logger(__name__)

GRAPH_BASE: Final = "https://graph.microsoft.com/v1.0"
DEFAULT_TIMEOUT: Final = httpx.Timeout(30.0, connect=10.0)
#: Graph caps list pages at 999 items.
MAX_PAGE_SIZE: Final = 999


class SharePointReadClient:
    """Reads lists and items from any SharePoint site in the tenant.

    Every request goes through :meth:`_get`, which routes through the guard. Even though this
    class only ever issues GETs, the guard call is kept: it means a future refactor that
    somehow introduces a write still hits the runtime check rather than sailing past it.
    """

    def __init__(
        self,
        *,
        access_token: str,
        guard: SharePointWriteGuard,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._token = access_token
        self._guard = guard
        self._http = http or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
        self._owns_http = http is None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # ── the single egress point ────────────────────────────────────────

    async def _get(self, url: str, *, site_id: str = "", **params: Any) -> dict[str, Any]:
        self._guard.check("GET", site_id or "n/a")

        response = await self._http.get(
            url if url.startswith("http") else f"{GRAPH_BASE}{url}",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
            },
            params={k: v for k, v in params.items() if v is not None} or None,
        )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        return payload

    # ── reads ──────────────────────────────────────────────────────────

    async def get_site_id(self, domain: str, site_path: str) -> str:
        """Resolve a site path to its opaque, stable site ID.

        The guard compares IDs rather than paths, so this is also how the sandbox allowlist
        entry is established at startup.
        """
        data = await self._get(f"/sites/{domain}:{site_path}")
        site_id: str = data["id"]
        return site_id

    async def get_list_id(self, site_id: str, list_name: str) -> str:
        data = await self._get(
            f"/sites/{site_id}/lists",
            site_id=site_id,
            **{"$filter": f"displayName eq '{list_name}'"},
        )
        items = data.get("value", [])
        if not items:
            raise LookupError(f"SharePoint list {list_name!r} not found on site {site_id!r}")
        list_id: str = items[0]["id"]
        return list_id

    async def get_list_columns(self, site_id: str, list_id: str) -> list[dict[str, Any]]:
        """Column definitions — used by the Phase 0 sandbox seeding task (§8.1.1)."""
        data = await self._get(f"/sites/{site_id}/lists/{list_id}/columns", site_id=site_id)
        columns: list[dict[str, Any]] = data.get("value", [])
        return columns

    async def iter_list_items(
        self, site_id: str, list_id: str, *, page_size: int = MAX_PAGE_SIZE
    ) -> list[dict[str, Any]]:
        """Every item in a list, following ``@odata.nextLink`` to the end.

        The legacy connector held its paging cursor in a module-level global, so each
        gunicorn worker resynced independently. Nothing is retained here between calls.
        """
        items: list[dict[str, Any]] = []
        url: str | None = f"/sites/{site_id}/lists/{list_id}/items"
        params: dict[str, Any] = {"$expand": "fields", "$top": page_size}

        while url:
            data = await self._get(url, site_id=site_id, **params)
            items.extend(data.get("value", []))
            url = data.get("@odata.nextLink")
            params = {}  # nextLink already carries the query string

        logger.debug("sharepoint.list_items_fetched", site_id=site_id, count=len(items))
        return items

    async def get_site_users(self, site_id: str) -> dict[str, dict[str, Any]]:
        """Map a site's person-field lookup IDs to their directory entries.

        Person columns do not come back as names. ``$expand=fields`` renders ``AssignedTo``
        as ``AssignedToLookupId`` holding an integer that is only meaningful against this
        site's hidden User Information List — which is what this reads. Keyed by lookup ID,
        values carry ``email`` and ``display_name``.

        Group entries (``Proposal Team Owners`` and friends) have no address and are skipped:
        a proposal is assigned to a person, not to a security group.
        """
        items = await self.iter_list_items(site_id, "User Information List")
        directory: dict[str, dict[str, Any]] = {}

        for item in items:
            fields = item.get("fields") or {}
            email = fields.get("EMail")
            if not email:
                continue
            directory[str(item.get("id"))] = {
                "email": str(email).lower(),
                "display_name": fields.get("Title"),
            }

        logger.debug("sharepoint.site_users_fetched", site_id=site_id, count=len(directory))
        return directory

    async def delta(
        self, site_id: str, list_id: str, delta_link: str | None = None
    ) -> tuple[list[dict[str, Any]], list[str], str | None]:
        """Incremental sync. Returns ``(changed, removed_ids, next_delta_link)``.

        The returned link is persisted to ``connector_status.cursor`` by the caller, not held
        in memory — that is what makes the sync safe to run from multiple workers.
        """
        url: str | None = delta_link or f"/sites/{site_id}/lists/{list_id}/items/delta"
        changed: list[dict[str, Any]] = []
        removed: list[str] = []
        next_link: str | None = None

        while url:
            data = await self._get(url, site_id=site_id)
            for entry in data.get("value", []):
                if "@removed" in entry:
                    removed.append(str(entry.get("id")))
                else:
                    changed.append(entry)

            url = data.get("@odata.nextLink")
            next_link = data.get("@odata.deltaLink") or next_link

        return changed, removed, next_link

    def status(self) -> dict[str, Any]:
        """Reported to ``connector_status`` and rendered in both panels."""
        return {"connector": "sharepoint", **self._guard.describe()}
