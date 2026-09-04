"""Reading the Proposals list out of SharePoint.

**This module is read-only.** SharePoint is the system of record for proposals;
people work in it directly, and nothing here writes back. Every call is a GET.

The awkward part is identity. SharePoint's ``AssignedTo`` is a person column, and
Graph hands it back as ``AssignedToLookupId`` — a number that means something only
within this one site (``"27"``), not an Entra object id or an email. Expanding the
field gives a display name (``"Goutham"``), which is no good for deciding who may
see a row: display names collide and change.

So the site's hidden *User Information List* is read once and cached, giving
``email -> lookupId``. That is what turns "the signed-in user" into "rows assigned
to them", and it is the only mapping the filter is allowed to rest on.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Final

import httpx

from app.core.config import Settings

GRAPH_BASE: Final = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE: Final = "https://graph.microsoft.com/.default"

_TOKEN_REFRESH_BUFFER_SECONDS: Final = 120
#: The site user list changes rarely; re-reading it per request would double the
#: cost of every call for no benefit.
_USERS_TTL_SECONDS: Final = 900
_PAGE_SIZE: Final = 200
_MAX_PAGES: Final = 25

#: AssignedToLookupId is not an indexed column, so SharePoint requires this
#: acknowledgement before it will filter on it.
_NON_INDEXED: Final = {"Prefer": "HonorNonIndexedQueriesWarningMayFailRandomly"}

#: Statuses that mean the work is finished. "My tasks" means the rest.
DONE_STATUSES: Final = frozenset({"completed"})

#: A row with no Status at all whose bid closed before today reads as this.
#: Nothing is written back to SharePoint — the list is live and read-only from
#: here. See ``ProposalTask.effective_status``.
EXPIRED: Final = "Expired"


class SharePointError(Exception):
    """SharePoint refused or could not be reached."""


@dataclass(frozen=True, slots=True)
class ProposalTask:
    """One row of the Proposals list, as this app cares about it."""

    id: str
    title: str
    status: str | None
    priority: str | None
    assigned_to_lookup_id: str | None
    assigned_to_name: str | None
    start_date: str | None
    due_date: str | None
    bid_closing_date: str | None
    end_user: str | None
    submission_status: str | None
    current_type: str | None
    order_status: str | None
    negotiation: str | None
    quote_no: str | None
    remarks: str | None
    working_notes: str | None
    created_at: str | None
    modified_at: str | None
    web_url: str | None

    @property
    def effective_status(self) -> str:
        """The status this row really has, which is not always the one stored.

        A fifth of the list has no Status at all. Treating those as live work is
        what made "open" meaningless: 270 rows, most of them bids that closed
        months ago, counted against whoever they were assigned to. If nobody ever
        set a status and the bid closing date has passed, the honest reading is
        that the bid expired.

        **Derived, never written.** SharePoint is the system of record and this
        module only reads it; the list is live and correcting 270 rows there is a
        decision for a person, not a side effect of an analytics call.

        A row with no status whose bid has *not* yet closed is left alone — that
        one really is outstanding work.
        """
        if (stored := (self.status or "").strip()):
            return stored
        closing = self.bid_closing_date or self.due_date
        if closing and closing < datetime.now(UTC).isoformat():
            return EXPIRED
        return ""

    @property
    def closing_date(self) -> date | None:
        """The bid closing date as a real date, for comparing against today."""
        raw = self.bid_closing_date or self.due_date
        if not raw:
            return None
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).date()
        except ValueError:
            return None

    @property
    def is_active(self) -> bool:
        """Work that can still be done: not finished, and the bid has not closed.

        **This is the number the assignment scoring uses.** A bid closes on its
        BCD and cannot be submitted to afterwards, so a row whose closing date
        has passed is not somebody's current workload however it is marked. The
        list bears this out: 452 of 461 not-completed rows have a closing date in
        the past, some as far back as May 2025, which is why counting them made
        "open" and "overdue" mean nothing.

        Today counts as active — a bid closing this afternoon is still live.
        """
        if self.effective_status.casefold() in DONE_STATUSES:
            return False
        closing = self.closing_date
        return closing is not None and closing >= datetime.now(UTC).date()

    @property
    def is_open(self) -> bool:
        """Not finished, whatever its deadline.

        Broader than :attr:`is_active` and kept for the task *list*: somebody
        looking at their own proposals wants to see the one whose bid closed last
        week and still needs writing up. The scoring deliberately does not use
        this — see :attr:`is_active`.
        """
        status = self.effective_status
        if status == EXPIRED:
            return False
        return status.casefold() not in DONE_STATUSES

    @property
    def is_bid_closed(self) -> bool:
        """Not finished, but its closing date has passed. Counted, not hidden."""
        return self.is_open and not self.is_active

    @property
    def is_expired(self) -> bool:
        """Never given a status, and its bid closed. Counted, not hidden."""
        return self.effective_status == EXPIRED

    @property
    def deadline(self) -> str | None:
        """The date that actually matters.

        BCD is the bid closing date and is set on every row; DueDate is set on
        about a third and disagrees with BCD almost whenever both exist. So BCD
        leads, and DueDate is only a fallback.
        """
        return self.bid_closing_date or self.due_date

    @property
    def has_no_status(self) -> bool:
        """No Status stored, whatever it is now read as. Worth naming rather
        than guessing, and reported separately so the derivation is auditable."""
        return not (self.status or "").strip()

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> ProposalTask:
        f = item.get("fields", {}) or {}
        assigned = f.get("AssignedTo")
        return cls(
            id=str(item.get("id")),
            title=f.get("Title") or "(untitled)",
            status=f.get("Status"),
            priority=f.get("Priority"),
            assigned_to_lookup_id=(
                str(f["AssignedToLookupId"]) if f.get("AssignedToLookupId") is not None else None
            ),
            # Display name only — useful to show, never to authorise on.
            assigned_to_name=assigned if isinstance(assigned, str) else None,
            start_date=f.get("StartDate"),
            due_date=f.get("DueDate"),
            bid_closing_date=f.get("BCD"),
            end_user=f.get("EndUser"),
            submission_status=f.get("SubmissionStatus"),
            current_type=f.get("CurrentType"),
            order_status=f.get("OrderStatus"),
            negotiation=f.get("Negotiation"),
            quote_no=f.get("zohpquoteno"),
            remarks=f.get("Remarks"),
            working_notes=f.get("WorkingNotes"),
            created_at=f.get("Created"),
            modified_at=f.get("Modified"),
            web_url=item.get("webUrl"),
        )


#: The three columns an aggregate needs. Pulling the full field set for 1,291
#: rows costs several times as much for numbers nobody reads.
#: ``Created`` is here for the assignment scoring, which needs to know when
#: somebody was last given work. The list has no "assigned on" column, so the
#: row's creation date is the closest honest proxy — it is when the task
#: appeared, which for this list is when it was handed out.
_AGGREGATE_FIELDS: Final = "Status,AssignedToLookupId,BCD,Created"

#: The fields worth asking for. Requesting everything drags along two dozen
#: compliance and versioning columns nobody reads.
_FIELDS: Final = (
    "Title,Status,Priority,AssignedTo,AssignedToLookupId,StartDate,DueDate,BCD,"
    "EndUser,SubmissionStatus,CurrentType,OrderStatus,Negotiation,zohpquoteno,"
    "Remarks,WorkingNotes,Created,Modified"
)


class SharePointProposals:
    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self._settings = settings
        self._http = http
        self._token: str | None = None
        self._expires_at = 0.0
        self._users: dict[str, str] | None = None
        self._users_at = 0.0
        self._user_list_id: str | None = None

    # ── auth ───────────────────────────────────────────────────────────

    async def _access_token(self) -> str:
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
        )
        if response.status_code != 200:
            raise SharePointError(
                f"client-credentials token request failed ({response.status_code})"
            )
        payload = response.json()
        self._token = payload["access_token"]
        self._expires_at = (
            time.monotonic() + int(payload.get("expires_in", 3600))
            - _TOKEN_REFRESH_BUFFER_SECONDS
        )
        return self._token

    async def _get(self, url: str, params: dict[str, str] | None = None, **extra) -> dict:
        token = await self._access_token()
        headers = {"Authorization": f"Bearer {token}", **extra.pop("headers", {})}
        response = await self._http.get(url, params=params, headers=headers, **extra)
        if response.status_code == 401:
            self._token, self._expires_at = None, 0.0
            raise SharePointError("SharePoint rejected the application token")
        if response.status_code != 200:
            raise SharePointError(
                f"SharePoint returned {response.status_code}: {response.text[:300]}"
            )
        return response.json()

    @property
    def _site(self) -> str:
        return f"{GRAPH_BASE}/sites/{self._settings.sharepoint_site_id}"

    # ── who is who, inside this site ───────────────────────────────────

    async def _user_information_list_id(self) -> str:
        if self._user_list_id:
            return self._user_list_id
        # "system" must be in $select or Graph omits system lists entirely, and
        # the User Information List is one — the query silently returns nothing.
        payload = await self._get(
            f"{self._site}/lists",
            {"$select": "id,displayName,system", "$top": "200"},
        )
        for lst in payload.get("value", []):
            if lst.get("displayName") in ("User Information List", "Users"):
                self._user_list_id = lst["id"]
                return self._user_list_id
        raise SharePointError("This site has no User Information List")

    async def site_users(self, *, force: bool = False) -> dict[str, str]:
        """``email (lowercased) -> SharePoint lookup id`` for this site."""
        fresh = time.monotonic() - self._users_at < _USERS_TTL_SECONDS
        if self._users is not None and fresh and not force:
            return self._users

        list_id = await self._user_information_list_id()
        url: str | None = f"{self._site}/lists/{list_id}/items"
        params: dict[str, str] | None = {
            "$expand": "fields($select=Title,EMail)",
            "$top": str(_PAGE_SIZE),
        }

        mapping: dict[str, str] = {}
        for _ in range(_MAX_PAGES):
            if url is None:
                break
            payload = await self._get(url, params)
            for item in payload.get("value", []):
                email = (item.get("fields", {}) or {}).get("EMail")
                if email:
                    # Shared mailboxes appear more than once; first wins, and it
                    # does not matter which, since nobody signs in as one.
                    mapping.setdefault(email.strip().casefold(), str(item["id"]))
            url = payload.get("@odata.nextLink")
            params = None

        self._users = mapping
        self._users_at = time.monotonic()
        return mapping

    async def lookup_id_for(self, email: str) -> str | None:
        """The caller's id within this site, or None if they have never been added."""
        users = await self.site_users()
        found = users.get(email.strip().casefold())
        if found is None:
            # They may have been added since the cache was filled.
            users = await self.site_users(force=True)
            found = users.get(email.strip().casefold())
        return found

    # ── the list itself ────────────────────────────────────────────────

    async def tasks_assigned_to(
        self, lookup_id: str, *, limit: int = 200
    ) -> list[ProposalTask]:
        """Rows whose AssignedTo is this person.

        Filtered by SharePoint rather than in memory: the alternative is pulling
        the whole list on every request and discarding almost all of it, which
        would also mean briefly holding other people's rows in this process.
        """
        url: str | None = f"{self._site}/lists/{self._settings.sharepoint_proposals_list_id}/items"
        params: dict[str, str] | None = {
            "$expand": f"fields($select={_FIELDS})",
            "$filter": f"fields/AssignedToLookupId eq {int(lookup_id)}",
            "$top": str(min(limit, _PAGE_SIZE)),
        }

        tasks: list[ProposalTask] = []
        for _ in range(_MAX_PAGES):
            if url is None or len(tasks) >= limit:
                break
            payload = await self._get(url, params, headers=dict(_NON_INDEXED))
            tasks.extend(ProposalTask.from_item(i) for i in payload.get("value", []))
            url = payload.get("@odata.nextLink")
            params = None

        return tasks[:limit]

    async def all_tasks(self, *, fields: str | None = None) -> list[ProposalTask]:
        """Every row in the list.

        Only used by the admin aggregate. SharePoint cannot group or count
        server-side — ``$apply`` is accepted and silently ignored, ``$count`` is
        unsupported — so the rows have to be pulled and counted here. Asking for
        three columns instead of twenty is what keeps that near a second.
        """
        url: str | None = (
            f"{self._site}/lists/{self._settings.sharepoint_proposals_list_id}/items"
        )
        params: dict[str, str] | None = {
            "$expand": f"fields($select={fields or _AGGREGATE_FIELDS})",
            "$top": "999",
        }

        tasks: list[ProposalTask] = []
        for _ in range(_MAX_PAGES):
            if url is None:
                break
            payload = await self._get(url, params)
            tasks.extend(ProposalTask.from_item(i) for i in payload.get("value", []))
            url = payload.get("@odata.nextLink")
            params = None
        return tasks

    async def site_people(self) -> dict[str, dict[str, str]]:
        """``lookupId -> {name, email}`` — the reverse of :meth:`site_users`."""
        list_id = await self._user_information_list_id()
        url: str | None = f"{self._site}/lists/{list_id}/items"
        params: dict[str, str] | None = {
            "$expand": "fields($select=Title,EMail)",
            "$top": str(_PAGE_SIZE),
        }

        people: dict[str, dict[str, str]] = {}
        for _ in range(_MAX_PAGES):
            if url is None:
                break
            payload = await self._get(url, params)
            for item in payload.get("value", []):
                f = item.get("fields", {}) or {}
                people[str(item["id"])] = {
                    "name": f.get("Title") or "",
                    "email": (f.get("EMail") or "").casefold(),
                }
            url = payload.get("@odata.nextLink")
            params = None
        return people

    async def list_columns(self) -> list[dict[str, Any]]:
        """The list's own schema, so a client can render unknown columns."""
        payload = await self._get(
            f"{self._site}/lists/{self._settings.sharepoint_proposals_list_id}/columns"
        )
        return [
            {
                "name": c.get("name"),
                "display_name": c.get("displayName"),
                "choices": (c.get("choice") or {}).get("choices"),
            }
            for c in payload.get("value", [])
            if not c.get("hidden") and not c.get("readOnly")
        ]
