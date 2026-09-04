"""Microsoft Graph, called as the application rather than as a user.

The auth module signs *a person* in. This one asks Graph about *everyone*, which
no individual's token can do — so it uses the client-credentials grant and the
``User.Read.All`` application permission already consented on the registration.

Two consequences worth keeping in mind:

* There is no user context. Every call has the same reach regardless of who
  triggered it, so authorization has to be enforced on our endpoints, not by
  Graph.
* The token is per-application and lasts an hour, so it is cached. Requesting a
  fresh one per call would add a round trip to every request for no benefit.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Final

import httpx

from app.core.config import Settings

GRAPH_BASE: Final = "https://graph.microsoft.com/v1.0"
#: ".default" means "every application permission already consented", which is
#: how the client-credentials grant expresses scope.
GRAPH_SCOPE: Final = "https://graph.microsoft.com/.default"

#: Renew slightly early so a request never sets off with a token that expires
#: mid-flight.
_TOKEN_REFRESH_BUFFER_SECONDS: Final = 120
#: Graph caps $top at 999 for the users collection.
_PAGE_SIZE: Final = 999
#: Guards against an unbounded loop if Graph keeps handing back nextLinks.
_MAX_PAGES: Final = 50

_USER_FIELDS: Final = (
    "id",
    "displayName",
    "givenName",
    "surname",
    "mail",
    "userPrincipalName",
    "jobTitle",
    "department",
    "officeLocation",
    "mobilePhone",
    "accountEnabled",
    "userType",
)


class GraphError(Exception):
    """Graph refused or could not be reached."""


@dataclass(frozen=True, slots=True)
class OrgUser:
    """One person as Entra knows them."""

    object_id: str
    display_name: str
    email: str | None
    user_principal_name: str
    job_title: str | None
    department: str | None
    office_location: str | None
    mobile_phone: str | None
    account_enabled: bool
    user_type: str | None

    @property
    def is_guest(self) -> bool:
        return (self.user_type or "").casefold() == "guest"

    @classmethod
    def from_graph(cls, raw: dict[str, Any]) -> OrgUser:
        return cls(
            object_id=raw["id"],
            display_name=raw.get("displayName") or raw.get("userPrincipalName") or raw["id"],
            # Accounts without a mailbox have no mail; the UPN always exists.
            email=(raw.get("mail") or "").lower() or None,
            user_principal_name=(raw.get("userPrincipalName") or "").lower(),
            job_title=raw.get("jobTitle"),
            department=raw.get("department"),
            office_location=raw.get("officeLocation"),
            mobile_phone=raw.get("mobilePhone"),
            account_enabled=bool(raw.get("accountEnabled", False)),
            user_type=raw.get("userType"),
        )


class GraphDirectory:
    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self._settings = settings
        self._http = http
        self._token: str | None = None
        self._expires_at = 0.0
        # Without this, a burst of concurrent requests on a cold cache would each
        # fetch their own token.
        self._token_lock = asyncio.Lock()

    # ── app-only token ─────────────────────────────────────────────────

    async def _access_token(self) -> str:
        if self._token and time.monotonic() < self._expires_at:
            return self._token

        async with self._token_lock:
            # Another coroutine may have refreshed it while we waited.
            if self._token and time.monotonic() < self._expires_at:
                return self._token

            response = await self._http.post(
                f"{self._settings.authority}/oauth2/v2.0/token",
                data={
                    "client_id": self._settings.azure_client_id,
                    "client_secret": self._settings.azure_client_secret,
                    "grant_type": "client_credentials",
                    "scope": GRAPH_SCOPE,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if response.status_code != 200:
                raise GraphError(
                    f"client-credentials token request failed "
                    f"({response.status_code}): {response.text}"
                )

            payload = response.json()
            token = payload.get("access_token")
            if not token:
                raise GraphError("token response contained no access_token")

            self._token = token
            self._expires_at = (
                time.monotonic() + int(payload.get("expires_in", 3600))
                - _TOKEN_REFRESH_BUFFER_SECONDS
            )
            return token

    async def _get(self, url: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        token = await self._access_token()
        response = await self._http.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        if response.status_code == 401:
            # The cached token was rejected — drop it so the next call re-auths
            # rather than repeating a doomed request.
            self._token, self._expires_at = None, 0.0
            raise GraphError("Graph rejected the application token")
        if response.status_code == 404:
            raise GraphError("not found")
        if response.status_code != 200:
            raise GraphError(f"Graph returned {response.status_code}: {response.text[:300]}")
        return response.json()

    # ── the directory ──────────────────────────────────────────────────

    async def list_users(
        self, *, include_guests: bool = False, include_disabled: bool = False
    ) -> list[OrgUser]:
        """Every user in the tenant, following Graph's paging to the end.

        Guests and disabled accounts are excluded by default: the ERP cares about
        current staff, and a directory listing that leads with a departed
        employee or an external collaborator is worse than useless for picking
        team members.
        """
        params = {"$select": ",".join(_USER_FIELDS), "$top": str(_PAGE_SIZE)}

        # userType is filterable server-side; accountEnabled is too, but combining
        # them needs $count/eventual consistency on some tenants. Filtering the
        # enabled flag in memory keeps the request simple and the result identical.
        if not include_guests:
            params["$filter"] = "userType eq 'Member'"

        users: list[OrgUser] = []
        url: str | None = f"{GRAPH_BASE}/users"
        page_params: dict[str, str] | None = params

        for _ in range(_MAX_PAGES):
            if url is None:
                break
            payload = await self._get(url, page_params)
            users.extend(OrgUser.from_graph(raw) for raw in payload.get("value", []))
            # nextLink already carries the query string; re-sending params would
            # duplicate it and Graph rejects that.
            url = payload.get("@odata.nextLink")
            page_params = None
        else:
            raise GraphError(f"directory listing exceeded {_MAX_PAGES} pages")

        if not include_disabled:
            users = [u for u in users if u.account_enabled]

        users.sort(key=lambda u: u.display_name.casefold())
        return users

    async def get_user(self, object_id: str) -> OrgUser:
        payload = await self._get(
            f"{GRAPH_BASE}/users/{object_id}", {"$select": ",".join(_USER_FIELDS)}
        )
        return OrgUser.from_graph(payload)
