"""The Graph calendar client, against a mock transport.

Nothing here reaches Microsoft. The transport returns canned Graph payloads, so
these cover the parts that are easy to get quietly wrong — the timezone the
timestamps come back in, who counts as an attendee, paging, token caching, and
telling a missing consent apart from an outage — without a network round trip.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from app.core.config import Settings
from app.meetings.calendar import (
    GRAPH_BASE,
    CalendarError,
    CalendarPermissionError,
    GraphCalendar,
    MailboxNotFoundError,
    Meeting,
)

WINDOW_START = datetime(2026, 9, 7, tzinfo=UTC)
WINDOW_END = datetime(2026, 9, 14, tzinfo=UTC)


def _settings() -> Settings:
    return Settings(
        azure_tenant_id="test-tenant",
        azure_client_id="test-client",
        azure_client_secret="test-secret",
    )


def _attendee(email: str, name: str, kind: str = "required", response: str = "accepted") -> dict:
    return {
        "type": kind,
        "status": {"response": response, "time": "2026-09-01T10:00:00.0000000Z"},
        "emailAddress": {"name": name, "address": email},
    }


def _raw(
    event_id: str = "evt-1",
    subject: str = "Design review",
    start: str = "2026-09-08T09:00:00.0000000",
    end: str = "2026-09-08T10:00:00.0000000",
    organizer: dict | None = None,
    attendees: list[dict] | None = None,
    **extra,
) -> dict:
    return {
        "id": event_id,
        "subject": subject,
        "bodyPreview": "Agenda attached.",
        "start": {"dateTime": start, "timeZone": "UTC"},
        "end": {"dateTime": end, "timeZone": "UTC"},
        "isAllDay": False,
        "isCancelled": False,
        "isOrganizer": False,
        "organizer": organizer
        if organizer is not None
        else {"emailAddress": {"name": "Alice", "address": "alice@hamdaz.com"}},
        "attendees": attendees if attendees is not None else [],
        "location": {"displayName": "Meeting Room 2"},
        "isOnlineMeeting": False,
        "responseStatus": {"response": "accepted"},
        "webLink": "https://outlook.office365.com/calendar/item/evt-1",
        **extra,
    }


class CalendarStub:
    """A mock transport that records what was asked of it."""

    def __init__(self, pages: list[dict] | None = None, token_status: int = 200) -> None:
        self.pages = pages if pages is not None else [{"value": []}]
        self.token_status = token_status
        self.token_requests = 0
        self.data_requests: list[httpx.Request] = []
        self.event_status = 200
        self.event_body: dict | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/v2.0/token"):
            self.token_requests += 1
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_client"})
            return httpx.Response(
                200, json={"access_token": f"token-{self.token_requests}", "expires_in": 3600}
            )

        self.data_requests.append(request)
        if self.event_status != 200:
            return httpx.Response(
                self.event_status, json=self.event_body or {"error": {"message": "nope"}}
            )

        index = len(self.data_requests) - 1
        return httpx.Response(200, json=self.pages[min(index, len(self.pages) - 1)])


def _client(stub: CalendarStub) -> GraphCalendar:
    http = httpx.AsyncClient(transport=httpx.MockTransport(stub.handler))
    return GraphCalendar(_settings(), http)


async def _list(stub: CalendarStub) -> list[Meeting]:
    return await _client(stub).list_meetings("oid-me", start=WINDOW_START, end=WINDOW_END)


# ── the request Graph is sent ──────────────────────────────────────────


async def test_reads_the_calendar_view_of_the_mailbox_it_was_given() -> None:
    stub = CalendarStub()
    await _list(stub)
    assert stub.data_requests[0].url.path == "/v1.0/users/oid-me/calendarView"


async def test_asks_for_the_window_it_was_given() -> None:
    stub = CalendarStub()
    await _list(stub)
    params = stub.data_requests[0].url.params

    assert params["startDateTime"].startswith("2026-09-07T00:00:00")
    assert params["endDateTime"].startswith("2026-09-14T00:00:00")


async def test_asks_graph_for_utc() -> None:
    """Without this header Graph answers in the mailbox's own timezone."""
    stub = CalendarStub()
    await _list(stub)
    assert stub.data_requests[0].headers["Prefer"] == 'outlook.timezone="UTC"'


async def test_asks_graph_to_order_by_start() -> None:
    stub = CalendarStub()
    await _list(stub)
    assert stub.data_requests[0].url.params["$orderby"] == "start/dateTime"


# ── what comes back ────────────────────────────────────────────────────


async def test_parses_a_meeting() -> None:
    stub = CalendarStub([{"value": [_raw()]}])
    meeting = (await _list(stub))[0]

    assert meeting.event_id == "evt-1"
    assert meeting.subject == "Design review"
    assert meeting.location == "Meeting Room 2"
    assert meeting.my_response == "accepted"


async def test_timestamps_come_back_timezone_aware() -> None:
    """Graph sends the zone as a sibling field, so a plain parse would be naive."""
    stub = CalendarStub([{"value": [_raw()]}])
    meeting = (await _list(stub))[0]

    assert meeting.start == datetime(2026, 9, 8, 9, 0, tzinfo=UTC)
    assert meeting.end == datetime(2026, 9, 8, 10, 0, tzinfo=UTC)
    assert meeting.start.tzinfo is not None


async def test_survives_graph_dropping_a_field() -> None:
    stub = CalendarStub([{"value": [{"id": "evt-9"}]}])
    meeting = (await _list(stub))[0]

    assert meeting.event_id == "evt-9"
    assert meeting.start is None
    assert meeting.attendees == ()


async def test_an_untitled_event_gets_a_readable_subject() -> None:
    stub = CalendarStub([{"value": [_raw(subject="")]}])
    assert (await _list(stub))[0].subject == "(No subject)"


async def test_an_event_without_an_id_is_skipped() -> None:
    """A row with no id could not be fetched again, so it is not offered."""
    stub = CalendarStub([{"value": [_raw(), {"subject": "orphan"}]}])
    assert [m.event_id for m in await _list(stub)] == ["evt-1"]


# ── who is in it ───────────────────────────────────────────────────────


async def test_lists_the_attendees() -> None:
    stub = CalendarStub(
        [
            {
                "value": [
                    _raw(
                        attendees=[
                            _attendee("bob@hamdaz.com", "Bob"),
                            _attendee("carol@hamdaz.com", "Carol", response="declined"),
                        ]
                    )
                ]
            }
        ]
    )
    meeting = (await _list(stub))[0]

    assert [a.name for a in meeting.attendees] == ["Alice", "Bob", "Carol"]
    assert [a.response for a in meeting.attendees] == ["organizer", "accepted", "declined"]


async def test_the_organizer_is_included_even_when_graph_omits_them() -> None:
    stub = CalendarStub([{"value": [_raw(attendees=[_attendee("bob@hamdaz.com", "Bob")])]}])
    meeting = (await _list(stub))[0]

    assert meeting.organizer is not None
    assert meeting.organizer.name == "Alice"
    assert [a.is_organizer for a in meeting.attendees] == [True, False]


async def test_the_organizer_is_not_listed_twice_when_graph_includes_them() -> None:
    """Graph puts the organizer in the attendee list on some events, not others."""
    stub = CalendarStub(
        [
            {
                "value": [
                    _raw(
                        attendees=[
                            _attendee("alice@hamdaz.com", "Alice"),
                            _attendee("bob@hamdaz.com", "Bob"),
                        ]
                    )
                ]
            }
        ]
    )
    meeting = (await _list(stub))[0]

    assert [a.name for a in meeting.attendees] == ["Alice", "Bob"]
    assert sum(a.is_organizer for a in meeting.attendees) == 1


async def test_the_organizer_match_is_case_insensitive() -> None:
    stub = CalendarStub(
        [{"value": [_raw(attendees=[_attendee("ALICE@Hamdaz.com", "Alice Smith")])]}]
    )
    meeting = (await _list(stub))[0]
    assert len(meeting.attendees) == 1
    assert meeting.attendees[0].is_organizer


async def test_rooms_are_attendees_but_are_not_counted_as_people() -> None:
    stub = CalendarStub(
        [
            {
                "value": [
                    _raw(
                        attendees=[
                            _attendee("bob@hamdaz.com", "Bob"),
                            _attendee("room2@hamdaz.com", "Meeting Room 2", kind="resource"),
                        ]
                    )
                ]
            }
        ]
    )
    meeting = (await _list(stub))[0]

    assert len(meeting.attendees) == 3  # organizer, Bob, the room
    assert meeting.attendee_count == 2  # the room is not a colleague
    assert [a.is_resource for a in meeting.attendees] == [False, False, True]


async def test_a_room_booking_alone_is_not_a_meeting() -> None:
    """A booked room with nobody invited is a reservation, not a meeting."""
    stub = CalendarStub(
        [{"value": [_raw(attendees=[_attendee("room2@hamdaz.com", "Room 2", kind="resource")])]}]
    )
    assert (await _list(stub))[0].has_attendees is False


async def test_an_appointment_with_nobody_on_it_is_not_a_meeting() -> None:
    stub = CalendarStub([{"value": [_raw(subject="Focus time", attendees=[])]}])
    assert (await _list(stub))[0].has_attendees is False


# ── recurrence and online meetings ─────────────────────────────────────


async def test_an_occurrence_reports_its_series() -> None:
    stub = CalendarStub(
        [{"value": [_raw(seriesMasterId="series-1", type="occurrence")]}]
    )
    meeting = (await _list(stub))[0]

    assert meeting.is_recurring
    assert meeting.series_master_id == "series-1"
    assert meeting.occurrence_type == "occurrence"


async def test_a_one_off_is_not_recurring() -> None:
    stub = CalendarStub([{"value": [_raw(type="singleInstance")]}])
    assert (await _list(stub))[0].is_recurring is False


async def test_picks_up_the_teams_join_link() -> None:
    stub = CalendarStub(
        [
            {
                "value": [
                    _raw(
                        isOnlineMeeting=True,
                        onlineMeetingProvider="teamsForBusiness",
                        onlineMeeting={"joinUrl": "https://teams.microsoft.com/l/meetup-join/x"},
                    )
                ]
            }
        ]
    )
    meeting = (await _list(stub))[0]

    assert meeting.is_online
    assert meeting.join_url == "https://teams.microsoft.com/l/meetup-join/x"
    assert meeting.online_provider == "teamsForBusiness"


# ── paging ─────────────────────────────────────────────────────────────


async def test_follows_the_next_link() -> None:
    stub = CalendarStub(
        [
            {
                "value": [_raw("evt-1")],
                "@odata.nextLink": f"{GRAPH_BASE}/users/oid-me/calendarView?$skiptoken=abc",
            },
            {"value": [_raw("evt-2")]},
        ]
    )
    assert [m.event_id for m in await _list(stub)] == ["evt-1", "evt-2"]


async def test_the_next_link_is_not_sent_the_params_again() -> None:
    """nextLink already carries the query string, and Graph rejects duplicates."""
    stub = CalendarStub(
        [
            {
                "value": [],
                "@odata.nextLink": f"{GRAPH_BASE}/users/oid-me/calendarView?$skiptoken=abc",
            },
            {"value": []},
        ]
    )
    await _list(stub)
    assert stub.data_requests[1].url.params.get_list("startDateTime") == []
    assert stub.data_requests[1].url.params["$skiptoken"] == "abc"


async def test_endless_paging_is_stopped() -> None:
    stub = CalendarStub(
        [
            {
                "value": [],
                "@odata.nextLink": f"{GRAPH_BASE}/users/oid-me/calendarView?$skiptoken=loop",
            }
        ]
    )
    with pytest.raises(CalendarError, match="exceeded"):
        await _list(stub)


# ── the token ──────────────────────────────────────────────────────────


async def test_the_token_is_fetched_once_and_reused() -> None:
    stub = CalendarStub()
    client = _client(stub)
    await client.list_meetings("oid-me", start=WINDOW_START, end=WINDOW_END)
    await client.list_meetings("oid-me", start=WINDOW_START, end=WINDOW_END)

    assert stub.token_requests == 1


async def test_concurrent_calls_on_a_cold_cache_fetch_one_token() -> None:
    import asyncio

    stub = CalendarStub()
    client = _client(stub)
    await asyncio.gather(
        *(client.list_meetings("oid-me", start=WINDOW_START, end=WINDOW_END) for _ in range(5))
    )
    assert stub.token_requests == 1


async def test_a_rejected_token_is_dropped_so_the_next_call_re_auths() -> None:
    stub = CalendarStub()
    client = _client(stub)
    stub.event_status = 401

    with pytest.raises(CalendarError):
        await client.list_meetings("oid-me", start=WINDOW_START, end=WINDOW_END)

    stub.event_status = 200
    await client.list_meetings("oid-me", start=WINDOW_START, end=WINDOW_END)
    assert stub.token_requests == 2


async def test_a_failed_token_request_is_a_calendar_error() -> None:
    stub = CalendarStub(token_status=401)
    with pytest.raises(CalendarError, match="token request failed"):
        await _list(stub)


# ── upstream failures worth telling apart ──────────────────────────────


async def test_a_denied_request_names_the_missing_permission() -> None:
    """403 means the consent is missing, which is a different job from an outage."""
    stub = CalendarStub()
    stub.event_status = 403

    with pytest.raises(CalendarPermissionError, match="Calendars.Read"):
        await _list(stub)


async def test_an_account_without_a_mailbox_is_its_own_error() -> None:
    stub = CalendarStub()
    stub.event_status = 404
    stub.event_body = {"error": {"code": "MailboxNotEnabledForRESTAPI", "message": "no mailbox"}}

    with pytest.raises(MailboxNotFoundError):
        await _list(stub)


async def test_a_plain_404_is_not_read_as_a_missing_mailbox() -> None:
    stub = CalendarStub()
    stub.event_status = 404
    stub.event_body = {"error": {"code": "ErrorItemNotFound", "message": "gone"}}

    with pytest.raises(CalendarError, match="not found") as caught:
        await _list(stub)
    assert not isinstance(caught.value, MailboxNotFoundError)


async def test_an_upstream_outage_is_a_calendar_error() -> None:
    stub = CalendarStub()
    stub.event_status = 503

    with pytest.raises(CalendarError, match="503"):
        await _list(stub)


# ── one meeting ────────────────────────────────────────────────────────


async def test_fetches_one_event_from_that_persons_mailbox() -> None:
    stub = CalendarStub([_raw("evt-7", subject="Kickoff")])
    meeting = await _client(stub).get_meeting("oid-me", "evt-7")

    assert meeting.event_id == "evt-7"
    assert meeting.subject == "Kickoff"
    assert stub.data_requests[0].url.path == "/v1.0/users/oid-me/events/evt-7"
