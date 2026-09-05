"""Response shapes for the meetings endpoints.

The listing and the single meeting deliberately return *different* models. A
week of calendar is dozens of events, and shipping every attendee list and body
preview inside it makes a payload that is mostly text nobody rendered. The
listing carries what a calendar view draws — when, what, where, who called it,
how many are coming — and the detail carries the invitation in full.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from app.meetings.calendar import Attendee, Meeting


class AttendeeOut(BaseModel):
    name: str
    email: str | None
    #: required | optional | resource
    kind: str
    #: none | organizer | tentativelyAccepted | accepted | declined | notResponded
    response: str
    responded_at: datetime | None
    is_organizer: bool
    #: A room or equipment rather than a person, so a UI can list it apart.
    is_resource: bool

    @classmethod
    def from_domain(cls, attendee: Attendee) -> AttendeeOut:
        return cls(
            name=attendee.name,
            email=attendee.email,
            kind=attendee.kind,
            response=attendee.response,
            responded_at=attendee.responded_at,
            is_organizer=attendee.is_organizer,
            is_resource=attendee.is_resource,
        )


class MeetingOut(BaseModel):
    """One row in the calendar listing."""

    #: Graph's event id, and what ``GET /meetings/{event_id}`` takes. For a
    #: recurring meeting this identifies *the occurrence*, not the series.
    event_id: str
    subject: str
    start: datetime | None
    end: datetime | None
    is_all_day: bool
    is_cancelled: bool
    is_organizer: bool
    organizer: AttendeeOut | None
    #: People invited, the organizer included, rooms excluded.
    attendee_count: int
    location: str | None
    is_online: bool
    join_url: str | None
    online_provider: str | None
    #: The caller's own answer to the invitation.
    my_response: str
    show_as: str | None
    is_recurring: bool
    web_link: str | None

    @classmethod
    def from_domain(cls, meeting: Meeting) -> MeetingOut:
        return cls(
            event_id=meeting.event_id,
            subject=meeting.subject,
            start=meeting.start,
            end=meeting.end,
            is_all_day=meeting.is_all_day,
            is_cancelled=meeting.is_cancelled,
            is_organizer=meeting.is_organizer,
            organizer=(
                AttendeeOut.from_domain(meeting.organizer) if meeting.organizer else None
            ),
            attendee_count=meeting.attendee_count,
            location=meeting.location,
            is_online=meeting.is_online,
            join_url=meeting.join_url,
            online_provider=meeting.online_provider,
            my_response=meeting.my_response,
            show_as=meeting.show_as,
            is_recurring=meeting.is_recurring,
            web_link=meeting.web_link,
        )


class MeetingDetailOut(MeetingOut):
    """One meeting in full, including everyone on it."""

    body_preview: str | None
    #: Everyone on the invitation — the organizer first, then required, optional
    #: and finally the rooms. Ordered rather than raw so a UI can render it
    #: without sorting, and so the organizer is never buried mid-list.
    attendees: list[AttendeeOut]
    sensitivity: str | None
    importance: str | None
    categories: list[str]
    #: Present when this is one occurrence of a recurring series.
    series_master_id: str | None
    #: singleInstance | occurrence | exception | seriesMaster
    occurrence_type: str | None
    last_modified_at: datetime | None

    @classmethod
    def from_domain(cls, meeting: Meeting) -> MeetingDetailOut:
        base = MeetingOut.from_domain(meeting)
        order = {"required": 1, "optional": 2, "resource": 3}
        attendees = sorted(
            meeting.attendees,
            key=lambda a: (0 if a.is_organizer else order.get(a.kind, 2), a.name.casefold()),
        )
        return cls(
            **base.model_dump(),
            body_preview=meeting.body_preview,
            attendees=[AttendeeOut.from_domain(a) for a in attendees],
            sensitivity=meeting.sensitivity,
            importance=meeting.importance,
            categories=list(meeting.categories),
            series_master_id=meeting.series_master_id,
            occurrence_type=meeting.occurrence_type,
            last_modified_at=meeting.last_modified_at,
        )


class MeetingPage(BaseModel):
    #: The window actually read, after the defaults were applied — echoed back
    #: so a caller that sent no dates knows what it got.
    window_start: datetime
    window_end: datetime
    #: Matches after filtering, before the window is applied.
    total: int
    #: How many are in this response.
    count: int
    offset: int
    limit: int
    meetings: list[MeetingOut]
