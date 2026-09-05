"""A person's calendar, read from Microsoft Graph as the application.

Same client-credentials grant as the directory, for the same reason: there is no
user token to borrow. A session cookie proves who is asking us, not who Entra
would let them be, so the reach here is the application's and authorization is
enforced by the router — every endpoint reads *the caller's own* mailbox and
nobody else's.

Two Graph choices worth recording, because both look arbitrary until they bite:

* **calendarView, not /events.** ``/events`` returns a recurring meeting as one
  series master with a recurrence rule, so "my Monday stand-up" appears once,
  dated whenever the series began, and never on the Monday that was asked about.
  ``calendarView`` expands the series into the occurrences inside the window,
  which is what a person means by "my meetings this week".
* **``Prefer: outlook.timezone="UTC"``.** Without it Graph answers in the
  mailbox's own timezone and stamps each event with that name, so one payload
  can mix zones and the naive datetimes underneath compare wrongly. Asking for
  UTC makes every timestamp directly comparable, and the client renders local.

The application permission needed is **Calendars.Read**. It is not the same
consent as the directory's User.Read.All — a tenant with one and not the other
gets a 403 here, which is why that case is reported distinctly rather than
folded into a generic upstream failure.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import httpx

from app.core.config import Settings

GRAPH_BASE: Final = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE: Final = "https://graph.microsoft.com/.default"

_TOKEN_REFRESH_BUFFER_SECONDS: Final = 120
#: Graph caps $top at 1000 for calendarView; 250 keeps a page small enough that
#: a wide window stays responsive.
_PAGE_SIZE: Final = 250
#: Guards against an unbounded loop if Graph keeps handing back nextLinks.
_MAX_PAGES: Final = 20

_EVENT_FIELDS: Final = (
    "id",
    "subject",
    "bodyPreview",
    "start",
    "end",
    "isAllDay",
    "isCancelled",
    "isOrganizer",
    "organizer",
    "attendees",
    "location",
    "isOnlineMeeting",
    "onlineMeetingProvider",
    "onlineMeeting",
    "responseStatus",
    "showAs",
    "sensitivity",
    "importance",
    "seriesMasterId",
    "type",
    "webLink",
    "categories",
    "lastModifiedDateTime",
)


class CalendarError(Exception):
    """Graph refused or could not be reached."""


class CalendarPermissionError(CalendarError):
    """Graph understood the request and said no.

    Separate from the general failure because the fix is different and known:
    grant and consent the Calendars.Read application permission. A 502 saying
    only "could not reach the calendar" sends someone hunting a network fault
    that is not there.
    """


class MailboxNotFoundError(CalendarError):
    """The account exists in Entra but has no Exchange mailbox to read.

    Service accounts and unlicensed users hit this. It is not a fault in the
    caller's request, so the listing answers it as an empty calendar rather than
    as a failure.
    """


def _graph_datetime(raw: Any) -> datetime | None:
    """Parse Graph's ``{dateTime, timeZone}`` pair into an aware UTC datetime.

    The value carries no offset — the zone is a sibling field — so a plain parse
    yields a naive datetime that would silently be treated as local time
    somewhere downstream. Since every call asks for UTC, the zone is attached
    here and the rest of the app never sees a naive timestamp.
    """
    if not isinstance(raw, dict):
        return None
    value = raw.get("dateTime")
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _timestamp(raw: Any) -> datetime | None:
    """Parse a plain ISO instant such as ``lastModifiedDateTime``."""
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class Attendee:
    """One person on a meeting, as the invitation records them.

    Deliberately not resolved against the directory. An attendee list routinely
    holds customers, consultants and distribution lists with no Entra account
    here, and dropping or failing on those would misreport who is in the room.
    What Graph knows — a name and an address — is what a caller needs in order
    to display the invitation.
    """

    name: str
    email: str | None
    #: required | optional | resource. Resources are rooms and equipment.
    kind: str
    #: none | organizer | tentativelyAccepted | accepted | declined | notResponded
    response: str
    responded_at: datetime | None
    is_organizer: bool

    @property
    def is_resource(self) -> bool:
        """A room or a projector, not a colleague."""
        return self.kind == "resource"

    @classmethod
    def from_graph(cls, raw: dict[str, Any], *, is_organizer: bool = False) -> Attendee:
        address = raw.get("emailAddress") or {}
        status = raw.get("status") or {}
        email = (address.get("address") or "").strip().lower() or None
        return cls(
            name=address.get("name") or email or "Unknown",
            email=email,
            kind=raw.get("type") or "required",
            # The organizer carries no status block; they are, by definition,
            # going.
            response="organizer" if is_organizer else (status.get("response") or "none"),
            responded_at=_timestamp(status.get("time")),
            is_organizer=is_organizer,
        )


@dataclass(frozen=True, slots=True)
class Meeting:
    """One occurrence on a calendar."""

    event_id: str
    subject: str
    body_preview: str | None
    start: datetime | None
    end: datetime | None
    is_all_day: bool
    is_cancelled: bool
    #: True when the calendar's owner called the meeting.
    is_organizer: bool
    organizer: Attendee | None
    attendees: tuple[Attendee, ...]
    location: str | None
    is_online: bool
    join_url: str | None
    online_provider: str | None
    #: The owner's own answer to the invitation.
    my_response: str
    show_as: str | None
    sensitivity: str | None
    importance: str | None
    #: Set when this is one occurrence of a recurring series.
    series_master_id: str | None
    #: singleInstance | occurrence | exception | seriesMaster
    occurrence_type: str | None
    web_link: str | None
    categories: tuple[str, ...]
    last_modified_at: datetime | None

    @property
    def is_recurring(self) -> bool:
        return bool(self.series_master_id)

    @property
    def has_attendees(self) -> bool:
        """Whether anyone besides the owner is involved.

        This is the line between a meeting and a personal appointment. Rooms do
        not count: a booked room with nobody in it is a reservation.
        """
        return any(not a.is_resource and not a.is_organizer for a in self.attendees)

    @property
    def attendee_count(self) -> int:
        """People on the invitation, the organizer included, rooms excluded."""
        return sum(1 for a in self.attendees if not a.is_resource)

    @classmethod
    def from_graph(cls, raw: dict[str, Any]) -> Meeting:
        organizer_raw = raw.get("organizer")
        organizer = (
            Attendee.from_graph(organizer_raw, is_organizer=True)
            if isinstance(organizer_raw, dict)
            else None
        )
        organizer_email = organizer.email if organizer else None

        attendees = [
            # Graph lists the organizer among the attendees on some events and
            # not on others. Matching on the address keeps the flag truthful
            # either way, and the organizer is prepended below when it was left
            # out — so "who is in it" never depends on which shape arrived.
            Attendee.from_graph(
                item,
                is_organizer=bool(
                    organizer_email
                    and ((item.get("emailAddress") or {}).get("address") or "").strip().lower()
                    == organizer_email
                ),
            )
            for item in raw.get("attendees") or []
            if isinstance(item, dict)
        ]
        if organizer and not any(a.is_organizer for a in attendees):
            attendees.insert(0, organizer)

        online = raw.get("onlineMeeting") or {}
        location = (raw.get("location") or {}).get("displayName") or None
        response_status = raw.get("responseStatus") or {}

        return cls(
            event_id=raw["id"],
            # An untitled event is legal in Outlook and shows there as "(No
            # subject)"; an empty string in a list is just a missing row.
            subject=(raw.get("subject") or "").strip() or "(No subject)",
            body_preview=(raw.get("bodyPreview") or "").strip() or None,
            start=_graph_datetime(raw.get("start")),
            end=_graph_datetime(raw.get("end")),
            is_all_day=bool(raw.get("isAllDay", False)),
            is_cancelled=bool(raw.get("isCancelled", False)),
            is_organizer=bool(raw.get("isOrganizer", False)),
            organizer=organizer,
            attendees=tuple(attendees),
            location=location.strip() if location else None,
            is_online=bool(raw.get("isOnlineMeeting", False)),
            join_url=online.get("joinUrl") or None,
            online_provider=raw.get("onlineMeetingProvider") or None,
            my_response=response_status.get("response") or "none",
            show_as=raw.get("showAs") or None,
            sensitivity=raw.get("sensitivity") or None,
            importance=raw.get("importance") or None,
            series_master_id=raw.get("seriesMasterId") or None,
            occurrence_type=raw.get("type") or None,
            web_link=raw.get("webLink") or None,
            categories=tuple(raw.get("categories") or []),
            last_modified_at=_timestamp(raw.get("lastModifiedDateTime")),
        )


class GraphCalendar:
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
                raise CalendarError(
                    f"client-credentials token request failed "
                    f"({response.status_code}): {response.text}"
                )

            payload = response.json()
            token = payload.get("access_token")
            if not token:
                raise CalendarError("token response contained no access_token")

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
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                # See the module docstring — this is what makes the timestamps
                # comparable to one another.
                "Prefer": 'outlook.timezone="UTC"',
            },
        )
        if response.status_code == 401:
            # The cached token was rejected — drop it so the next call re-auths
            # rather than repeating a doomed request.
            self._token, self._expires_at = None, 0.0
            raise CalendarError("Graph rejected the application token")
        if response.status_code == 403:
            raise CalendarPermissionError(
                "Graph denied the calendar request; the Calendars.Read "
                "application permission is most likely not consented"
            )
        if response.status_code == 404:
            # Graph answers 404 both for "no such event" and for an account with
            # no mailbox, and those mean very different things to a caller.
            if "MailboxNotEnabled" in response.text:
                raise MailboxNotFoundError("that account has no mailbox")
            raise CalendarError("not found")
        if response.status_code != 200:
            raise CalendarError(f"Graph returned {response.status_code}: {response.text[:300]}")
        return response.json()

    # ── the calendar ───────────────────────────────────────────────────

    async def list_meetings(self, user_id: str, *, start: datetime, end: datetime) -> list[Meeting]:
        """Everything on one person's calendar between two instants.

        Recurring series arrive already expanded into their occurrences, and the
        result is in start order — Graph's own ordering, which it will only apply
        to calendarView because the window makes the set finite.
        """
        params = {
            "startDateTime": start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "endDateTime": end.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "$select": ",".join(_EVENT_FIELDS),
            "$orderby": "start/dateTime",
            "$top": str(_PAGE_SIZE),
        }

        meetings: list[Meeting] = []
        url: str | None = f"{GRAPH_BASE}/users/{user_id}/calendarView"
        page_params: dict[str, str] | None = params

        for _ in range(_MAX_PAGES):
            if url is None:
                break
            payload = await self._get(url, page_params)
            meetings.extend(
                Meeting.from_graph(raw)
                for raw in payload.get("value", [])
                if isinstance(raw, dict) and raw.get("id")
            )
            # nextLink already carries the query string; re-sending params would
            # duplicate it and Graph rejects that.
            url = payload.get("@odata.nextLink")
            page_params = None
        else:
            raise CalendarError(f"calendar listing exceeded {_MAX_PAGES} pages")

        return meetings

    async def get_meeting(self, user_id: str, event_id: str) -> Meeting:
        """One event from that person's calendar, with its full attendee list."""
        payload = await self._get(
            f"{GRAPH_BASE}/users/{user_id}/events/{event_id}",
            {"$select": ",".join(_EVENT_FIELDS)},
        )
        return Meeting.from_graph(payload)
