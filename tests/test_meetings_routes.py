"""The meetings HTTP surface: whose calendar, what window, and what filters.

The client's own parsing and paging are covered against a mock transport in
test_meetings.py. What matters here is the part only the router can get wrong —
above all that it reads the caller's mailbox and never one named by a request.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.auth.deps import SESSION_AUDIENCE
from app.auth.oidc import EntraIdentity
from app.auth.service import upsert_user
from app.core.config import get_settings
from app.core.security import sign
from app.meetings.calendar import (
    Attendee,
    CalendarError,
    CalendarPermissionError,
    MailboxNotFoundError,
    Meeting,
)

SESSION_COOKIE = "hamdaz_session"


def _attendee(name: str, **over) -> Attendee:
    return Attendee(
        **{
            "name": name,
            "email": f"{name.lower().replace(' ', '.')}@hamdaz.com",
            "kind": "required",
            "response": "accepted",
            "responded_at": None,
            "is_organizer": False,
            **over,
        }
    )


def _meeting(event_id: str = "evt-1", subject: str = "Design review", **over) -> Meeting:
    organizer = over.pop("organizer", _attendee("Alice", response="organizer", is_organizer=True))
    attendees = over.pop("attendees", (organizer, _attendee("Bob")))
    return Meeting(
        **{
            "event_id": event_id,
            "subject": subject,
            "body_preview": "Agenda attached.",
            "start": datetime(2026, 9, 8, 9, 0, tzinfo=UTC),
            "end": datetime(2026, 9, 8, 10, 0, tzinfo=UTC),
            "is_all_day": False,
            "is_cancelled": False,
            "is_organizer": False,
            "organizer": organizer,
            "attendees": tuple(attendees),
            "location": "Meeting Room 2",
            "is_online": False,
            "join_url": None,
            "online_provider": None,
            "my_response": "accepted",
            "show_as": "busy",
            "sensitivity": "normal",
            "importance": "normal",
            "series_master_id": None,
            "occurrence_type": "singleInstance",
            "web_link": "https://outlook.office365.com/calendar/item/evt-1",
            "categories": (),
            "last_modified_at": None,
            **over,
        }
    )


@pytest.fixture
async def authed(client, db):
    user = await upsert_user(
        db, EntraIdentity(object_id="oid-me", email="me@hamdaz.com", display_name="Me")
    )
    await db.commit()
    client.cookies.set(
        SESSION_COOKIE,
        sign(
            {"sub": str(user.id)},
            secret=get_settings().session_secret,
            ttl_minutes=60,
            audience=SESSION_AUDIENCE,
        ),
    )
    return client


# ── authorization ──────────────────────────────────────────────────────


async def test_listing_requires_a_session(client) -> None:
    assert (await client.get("/api/v1/meetings")).status_code == 401


async def test_single_meeting_requires_a_session(client) -> None:
    assert (await client.get("/api/v1/meetings/evt-1")).status_code == 401


async def test_the_calendar_is_not_reached_without_a_session(client, calendar) -> None:
    """Auth must short-circuit before we spend a Graph call."""
    await client.get("/api/v1/meetings")
    assert calendar.calls == []


async def test_it_reads_the_callers_own_mailbox(authed, calendar) -> None:
    """The whole security model: the mailbox comes from the session."""
    await authed.get("/api/v1/meetings")
    assert calendar.calls[0]["user_id"] == "oid-me"


async def test_no_query_parameter_can_redirect_it_to_another_mailbox(authed, calendar) -> None:
    """The app-only token could open any mailbox, so this must stay impossible."""
    await authed.get(
        "/api/v1/meetings",
        params={"user_id": "oid-someone-else", "mailbox": "ceo@hamdaz.com"},
    )
    assert calendar.calls[0]["user_id"] == "oid-me"


async def test_one_meeting_is_read_from_the_callers_own_mailbox(authed, calendar) -> None:
    calendar.meetings = [_meeting("evt-1")]
    await authed.get("/api/v1/meetings/evt-1")
    assert calendar.calls[0]["user_id"] == "oid-me"


# ── the window ─────────────────────────────────────────────────────────


async def test_defaults_to_today_and_the_week_ahead(authed, calendar) -> None:
    await authed.get("/api/v1/meetings")
    call = calendar.calls[0]
    today = datetime.now(UTC).date()

    assert call["start"].date() == today
    # Half-open: the 7th day ahead is included, so the bound is the day after.
    assert call["end"].date() == today + timedelta(days=8)


async def test_the_window_starts_at_midnight_not_now(authed, calendar) -> None:
    """A meeting already under way is the one a person is most likely after."""
    await authed.get("/api/v1/meetings")
    start = calendar.calls[0]["start"]
    assert (start.hour, start.minute, start.second) == (0, 0, 0)


async def test_the_window_is_sent_as_aware_utc(authed, calendar) -> None:
    await authed.get("/api/v1/meetings")
    assert calendar.calls[0]["start"].tzinfo is not None
    assert calendar.calls[0]["end"].tzinfo is not None


async def test_explicit_dates_are_honoured(authed, calendar) -> None:
    await authed.get("/api/v1/meetings", params={"start": "2026-09-07", "end": "2026-09-09"})
    call = calendar.calls[0]

    assert call["start"] == datetime(2026, 9, 7, tzinfo=UTC)
    # The end date is inclusive of its whole day.
    assert call["end"] == datetime(2026, 9, 10, tzinfo=UTC)


async def test_a_single_day_is_that_whole_day(authed, calendar) -> None:
    """start == end must mean the day, not an empty span."""
    await authed.get("/api/v1/meetings", params={"start": "2026-09-08", "end": "2026-09-08"})
    call = calendar.calls[0]

    assert call["start"] == datetime(2026, 9, 8, tzinfo=UTC)
    assert call["end"] == datetime(2026, 9, 9, tzinfo=UTC)


async def test_a_start_without_an_end_gets_the_week_after_it(authed, calendar) -> None:
    await authed.get("/api/v1/meetings", params={"start": "2026-09-07"})
    assert calendar.calls[0]["end"] == datetime(2026, 9, 15, tzinfo=UTC)


async def test_a_backwards_window_is_refused(authed, calendar) -> None:
    response = await authed.get(
        "/api/v1/meetings", params={"start": "2026-09-10", "end": "2026-09-01"}
    )
    assert response.status_code == 422
    assert calendar.calls == []


async def test_an_enormous_window_is_refused(authed, calendar) -> None:
    response = await authed.get(
        "/api/v1/meetings", params={"start": "2020-01-01", "end": "2026-01-01"}
    )
    assert response.status_code == 422
    assert calendar.calls == []


async def test_a_nonsense_date_is_a_422(authed) -> None:
    response = await authed.get("/api/v1/meetings", params={"start": "last tuesday"})
    assert response.status_code == 422


async def test_the_window_actually_read_is_echoed_back(authed, calendar) -> None:
    """A caller that sent no dates still needs to know what it got."""
    body = (await authed.get("/api/v1/meetings")).json()
    assert body["window_start"].startswith(str(datetime.now(UTC).date()))
    assert body["window_end"] is not None


# ── the listing ────────────────────────────────────────────────────────


async def test_returns_the_meetings(authed, calendar) -> None:
    calendar.meetings = [_meeting("evt-1", "Design review"), _meeting("evt-2", "Site visit")]
    response = await authed.get("/api/v1/meetings")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert [m["subject"] for m in body["meetings"]] == ["Design review", "Site visit"]


async def test_a_row_carries_who_called_it_and_how_many_are_coming(authed, calendar) -> None:
    calendar.meetings = [_meeting()]
    row = (await authed.get("/api/v1/meetings")).json()["meetings"][0]

    assert row["organizer"]["name"] == "Alice"
    assert row["attendee_count"] == 2
    assert row["event_id"] == "evt-1"


async def test_the_listing_stays_lean(authed, calendar) -> None:
    """Attendee lists and bodies belong on the detail, not in a week of rows."""
    calendar.meetings = [_meeting()]
    row = (await authed.get("/api/v1/meetings")).json()["meetings"][0]

    assert "attendees" not in row
    assert "body_preview" not in row


async def test_an_empty_calendar_is_not_an_error(authed, calendar) -> None:
    body = (await authed.get("/api/v1/meetings")).json()
    assert body["meetings"] == []
    assert body["total"] == 0


# ── filters ────────────────────────────────────────────────────────────


async def test_cancelled_meetings_are_hidden_by_default(authed, calendar) -> None:
    calendar.meetings = [_meeting("evt-1"), _meeting("evt-2", is_cancelled=True)]
    body = (await authed.get("/api/v1/meetings")).json()

    assert [m["event_id"] for m in body["meetings"]] == ["evt-1"]


async def test_cancelled_meetings_can_be_asked_for(authed, calendar) -> None:
    calendar.meetings = [_meeting("evt-1"), _meeting("evt-2", is_cancelled=True)]
    body = (await authed.get("/api/v1/meetings", params={"include_cancelled": "true"})).json()

    assert body["total"] == 2


async def test_appointments_are_included_by_default(authed, calendar) -> None:
    """The calendar is the calendar; narrowing it is opt-in."""
    calendar.meetings = [_meeting("evt-1"), _meeting("evt-2", "Focus time", attendees=())]
    body = (await authed.get("/api/v1/meetings")).json()
    assert body["total"] == 2


async def test_meetings_only_drops_solo_appointments(authed, calendar) -> None:
    calendar.meetings = [
        _meeting("evt-1", "Design review"),
        _meeting("evt-2", "Focus time", attendees=(), organizer=None),
    ]
    body = (await authed.get("/api/v1/meetings", params={"meetings_only": "true"})).json()

    assert [m["subject"] for m in body["meetings"]] == ["Design review"]


async def test_meetings_only_drops_a_lone_room_booking(authed, calendar) -> None:
    room = _attendee("Room 2", kind="resource")
    organizer = _attendee("Me", response="organizer", is_organizer=True)
    calendar.meetings = [
        _meeting("evt-1", "Desk booking", organizer=organizer, attendees=(organizer, room))
    ]
    body = (await authed.get("/api/v1/meetings", params={"meetings_only": "true"})).json()

    assert body["meetings"] == []


@pytest.mark.parametrize(
    "term,expected",
    [
        ("design", ["Design review"]),
        ("DESIGN", ["Design review"]),  # case-insensitive
        ("room 2", ["Design review"]),  # matches location
        ("bob", ["Design review"]),  # matches an attendee
        ("bob@hamdaz.com", ["Design review"]),  # matches their address
        ("alice", ["Design review", "Site visit"]),  # matches the organizer
        ("nobody", []),
    ],
)
async def test_search_matches_subject_location_and_people(
    authed, calendar, term: str, expected: list
) -> None:
    calendar.meetings = [
        _meeting("evt-1", "Design review"),
        _meeting("evt-2", "Site visit", location="Jebel Ali", attendees=()),
    ]
    body = (await authed.get("/api/v1/meetings", params={"search": term})).json()
    assert [m["subject"] for m in body["meetings"]] == expected


async def test_search_ignores_surrounding_whitespace(authed, calendar) -> None:
    calendar.meetings = [_meeting()]
    body = (await authed.get("/api/v1/meetings", params={"search": "  design  "})).json()
    assert body["total"] == 1


# ── paging ─────────────────────────────────────────────────────────────


async def test_total_counts_matches_not_the_window(authed, calendar) -> None:
    calendar.meetings = [_meeting(f"evt-{i}", f"Meeting {i:02d}") for i in range(10)]
    body = (await authed.get("/api/v1/meetings", params={"limit": 3})).json()

    assert body["total"] == 10
    assert body["count"] == 3
    assert len(body["meetings"]) == 3


async def test_offset_windows_do_not_overlap(authed, calendar) -> None:
    calendar.meetings = [_meeting(f"evt-{i}", f"Meeting {i:02d}") for i in range(10)]

    first = (await authed.get("/api/v1/meetings", params={"limit": 4, "offset": 0})).json()
    second = (await authed.get("/api/v1/meetings", params={"limit": 4, "offset": 4})).json()

    ids = {m["event_id"] for m in first["meetings"]}
    assert ids.isdisjoint({m["event_id"] for m in second["meetings"]})


async def test_offset_past_the_end_returns_empty_not_an_error(authed, calendar) -> None:
    calendar.meetings = [_meeting()]
    body = (await authed.get("/api/v1/meetings", params={"offset": 500})).json()

    assert body["meetings"] == []
    assert body["total"] == 1


async def test_total_reflects_the_filter_not_the_calendar(authed, calendar) -> None:
    calendar.meetings = [_meeting("evt-1", "Design review"), _meeting("evt-2", "Site visit")]
    body = (await authed.get("/api/v1/meetings", params={"search": "design"})).json()
    assert body["total"] == 1


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 500}, {"offset": -1}])
async def test_rejects_nonsense_paging(authed, params: dict) -> None:
    assert (await authed.get("/api/v1/meetings", params=params)).status_code == 422


# ── one meeting, and everyone on it ────────────────────────────────────


async def test_fetches_one_meeting(authed, calendar) -> None:
    calendar.meetings = [_meeting("evt-1", "Kickoff")]
    response = await authed.get("/api/v1/meetings/evt-1")

    assert response.status_code == 200
    assert response.json()["subject"] == "Kickoff"


async def test_the_detail_lists_everyone_on_it(authed, calendar) -> None:
    calendar.meetings = [_meeting()]
    body = (await authed.get("/api/v1/meetings/evt-1")).json()

    assert [a["name"] for a in body["attendees"]] == ["Alice", "Bob"]
    assert [a["email"] for a in body["attendees"]] == ["alice@hamdaz.com", "bob@hamdaz.com"]


async def test_the_detail_carries_each_persons_answer(authed, calendar) -> None:
    organizer = _attendee("Alice", response="organizer", is_organizer=True)
    calendar.meetings = [
        _meeting(
            organizer=organizer,
            attendees=(
                organizer,
                _attendee("Bob", response="declined"),
                _attendee("Carol", response="notResponded"),
            ),
        )
    ]
    body = (await authed.get("/api/v1/meetings/evt-1")).json()

    assert {a["name"]: a["response"] for a in body["attendees"]} == {
        "Alice": "organizer",
        "Bob": "declined",
        "Carol": "notResponded",
    }


async def test_the_organizer_comes_first_then_required_optional_and_rooms(
    authed, calendar
) -> None:
    organizer = _attendee("Zara", response="organizer", is_organizer=True)
    calendar.meetings = [
        _meeting(
            organizer=organizer,
            attendees=(
                _attendee("Room 2", kind="resource"),
                _attendee("Bob", kind="optional"),
                _attendee("Adam"),
                organizer,
            ),
        )
    ]
    body = (await authed.get("/api/v1/meetings/evt-1")).json()

    assert [a["name"] for a in body["attendees"]] == ["Zara", "Adam", "Bob", "Room 2"]


async def test_rooms_are_flagged_so_a_ui_can_separate_them(authed, calendar) -> None:
    organizer = _attendee("Alice", response="organizer", is_organizer=True)
    calendar.meetings = [
        _meeting(organizer=organizer, attendees=(organizer, _attendee("Room 2", kind="resource")))
    ]
    body = (await authed.get("/api/v1/meetings/evt-1")).json()

    assert {a["name"]: a["is_resource"] for a in body["attendees"]} == {
        "Alice": False,
        "Room 2": True,
    }


async def test_the_detail_adds_the_body_and_the_series(authed, calendar) -> None:
    calendar.meetings = [
        _meeting(series_master_id="series-1", occurrence_type="occurrence")
    ]
    body = (await authed.get("/api/v1/meetings/evt-1")).json()

    assert body["body_preview"] == "Agenda attached."
    assert body["series_master_id"] == "series-1"
    assert body["is_recurring"] is True


async def test_the_detail_keeps_the_join_link(authed, calendar) -> None:
    calendar.meetings = [
        _meeting(
            is_online=True,
            join_url="https://teams.microsoft.com/l/meetup-join/x",
            online_provider="teamsForBusiness",
        )
    ]
    body = (await authed.get("/api/v1/meetings/evt-1")).json()

    assert body["is_online"] is True
    assert body["join_url"] == "https://teams.microsoft.com/l/meetup-join/x"


async def test_a_meeting_not_on_your_calendar_is_404(authed, calendar) -> None:
    """An id copied from somewhere else must reveal nothing."""
    calendar.meetings = []
    assert (await authed.get("/api/v1/meetings/evt-nope")).status_code == 404


# ── upstream failure ───────────────────────────────────────────────────


async def test_graph_failure_is_a_502_not_a_500(authed, calendar) -> None:
    calendar.error = CalendarError("Graph returned 503")
    assert (await authed.get("/api/v1/meetings")).status_code == 502


async def test_graph_failure_does_not_leak_internals(authed, calendar) -> None:
    calendar.error = CalendarError("token request failed: secret expired")
    body = (await authed.get("/api/v1/meetings")).json()
    assert "secret" not in body["detail"]


async def test_a_missing_consent_says_which_permission_to_grant(authed, calendar) -> None:
    """The fix is known and specific, so the message should name it."""
    calendar.error = CalendarPermissionError("denied")
    body = (await authed.get("/api/v1/meetings")).json()
    assert "Calendars.Read" in body["detail"]


async def test_an_account_without_a_mailbox_gets_an_empty_calendar(authed, calendar) -> None:
    """An unlicensed user should see an empty week, not an error they cannot act on."""
    calendar.error = MailboxNotFoundError("no mailbox")
    response = await authed.get("/api/v1/meetings")

    assert response.status_code == 200
    assert response.json()["meetings"] == []


async def test_a_single_lookup_on_a_mailboxless_account_says_so(authed, calendar) -> None:
    calendar.error = MailboxNotFoundError("no mailbox")
    response = await authed.get("/api/v1/meetings/evt-1")

    assert response.status_code == 404
    assert "mailbox" in response.json()["detail"]


async def test_graph_failure_on_a_single_lookup_is_a_502(authed, calendar) -> None:
    calendar.error = CalendarError("Graph returned 500")
    assert (await authed.get("/api/v1/meetings/evt-1")).status_code == 502
