"""Read the signed-in person's meetings.

Nothing here writes. Accepting a meeting, declining one or creating one all
belong to Outlook, and this module is a view onto what is already there.

**The authorization rule is the whole of the security model, so it is worth
stating plainly: every route reads ``user.entra_object_id`` — the caller's own
mailbox, taken from the session — and there is no parameter anywhere that names
whose calendar to read.** The Graph token is an application one and could open
any mailbox in the tenant (see app/meetings/calendar.py), so the only thing
standing between a colleague's calendar and a caller is that the mailbox is
never chosen by the request. A path or query parameter added here later would
remove that protection entirely; reading somebody else's calendar needs a
permission check first.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.auth.deps import CurrentUser
from app.meetings.calendar import (
    CalendarError,
    CalendarPermissionError,
    GraphCalendar,
    MailboxNotFoundError,
    Meeting,
)
from app.meetings.schemas import MeetingDetailOut, MeetingOut, MeetingPage

router = APIRouter(prefix="/meetings", tags=["meetings"])

#: What "my meetings" means with no dates given: today plus the week ahead.
#: Starting at midnight rather than now keeps a meeting that is already running
#: — the one a person is most likely looking for — in the answer.
_DEFAULT_DAYS_AHEAD = 7
#: A calendarView over years is a slow query for a caller who almost certainly
#: meant something smaller, and it is the one shape that can push the page loop
#: in the client to its limit.
_MAX_WINDOW_DAYS = 370

_UPSTREAM_UNAVAILABLE = "Could not reach the calendar"


def get_calendar(request: Request) -> GraphCalendar:
    return request.app.state.calendar


def _no_mailbox() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="There is no mailbox on this account, so it has no calendar",
    )


def _upstream(exc: CalendarError) -> HTTPException:
    """Turn a client failure into the answer the caller should act on.

    A missing consent is called out by name. Everything else becomes a 502 with
    a fixed message — 502 rather than 500 because the fault is upstream, and
    fixed because ``exc`` can carry a token response that should not travel.
    """
    if isinstance(exc, CalendarPermissionError):
        return HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "This application is not permitted to read calendars. "
                "Grant the Calendars.Read application permission in Entra ID."
            ),
        )
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=_UPSTREAM_UNAVAILABLE)


def _window(start: date | None, end: date | None) -> tuple[datetime, datetime]:
    """Resolve the requested dates into a half-open UTC range.

    Dates rather than instants, because "my meetings on Thursday" is the
    question people actually ask. ``end`` is inclusive of its whole day: a
    caller asking for the 5th to the 5th means that day, not the empty span
    between one midnight and itself.
    """
    today = datetime.now(UTC).date()
    first = start or today
    last = end or (first + timedelta(days=_DEFAULT_DAYS_AHEAD))

    if last < first:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="The end date is before the start date",
        )
    if (last - first).days > _MAX_WINDOW_DAYS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Ask for at most {_MAX_WINDOW_DAYS} days at a time",
        )

    return (
        datetime.combine(first, time.min, tzinfo=UTC),
        datetime.combine(last + timedelta(days=1), time.min, tzinfo=UTC),
    )


def _matches(meeting: Meeting, needle: str) -> bool:
    haystack = " ".join(
        part
        for part in (
            meeting.subject,
            meeting.location,
            meeting.organizer.name if meeting.organizer else None,
            *(a.name for a in meeting.attendees),
            *(a.email for a in meeting.attendees if a.email),
        )
        if part
    )
    return needle in haystack.casefold()


@router.get("", response_model=MeetingPage, summary="My meetings")
async def list_my_meetings(
    user: CurrentUser,
    calendar: Annotated[GraphCalendar, Depends(get_calendar)],
    start: Annotated[
        date | None, Query(description="First day to read. Defaults to today (UTC).")
    ] = None,
    end: Annotated[
        date | None,
        Query(description="Last day to read, inclusive. Defaults to a week after the start."),
    ] = None,
    search: Annotated[
        str | None, Query(description="Match subject, location, organizer or attendee")
    ] = None,
    meetings_only: Annotated[
        bool,
        Query(
            description=(
                "Only events with other people on them, "
                "excluding personal appointments and room bookings"
            )
        ),
    ] = False,
    include_cancelled: Annotated[
        bool, Query(description="Include meetings that were called off")
    ] = False,
    limit: Annotated[int, Query(ge=1, le=250)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> MeetingPage:
    window_start, window_end = _window(start, end)

    try:
        meetings = await calendar.list_meetings(
            user.entra_object_id, start=window_start, end=window_end
        )
    except MailboxNotFoundError:
        # Not a failure: the account simply has no calendar to show. Answering
        # with an empty week lets a UI render normally for an unlicensed user
        # instead of showing them an error they cannot act on.
        meetings = []
    except CalendarError as exc:
        raise _upstream(exc) from exc

    if not include_cancelled:
        meetings = [m for m in meetings if not m.is_cancelled]
    if meetings_only:
        meetings = [m for m in meetings if m.has_attendees]
    if search:
        needle = search.strip().casefold()
        meetings = [m for m in meetings if _matches(m, needle)]

    window = meetings[offset : offset + limit]
    return MeetingPage(
        window_start=window_start,
        window_end=window_end,
        total=len(meetings),
        count=len(window),
        offset=offset,
        limit=limit,
        meetings=[MeetingOut.from_domain(m) for m in window],
    )


@router.get(
    "/{event_id}",
    response_model=MeetingDetailOut,
    summary="One meeting, and everyone on it",
)
async def get_my_meeting(
    event_id: str,
    user: CurrentUser,
    calendar: Annotated[GraphCalendar, Depends(get_calendar)],
) -> MeetingDetailOut:
    """One meeting from the caller's own calendar.

    Reading it out of *their* mailbox is also what makes the 404 correct: an
    event id belonging to a meeting they were never invited to is not found
    here, so an id guessed or copied from elsewhere reveals nothing.
    """
    try:
        meeting = await calendar.get_meeting(user.entra_object_id, event_id)
    except MailboxNotFoundError as exc:
        raise _no_mailbox() from exc
    except CalendarError as exc:
        if "not found" in str(exc):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No such meeting on your calendar",
            ) from exc
        raise _upstream(exc) from exc

    return MeetingDetailOut.from_domain(meeting)
