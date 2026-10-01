"""The task calendar: an open task's BCD as an Outlook event, kept in step.

No database, list or Outlook — fakes for each.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from app.models.followup import FollowupSettings
from app.models.task_calendar import TaskCalendarEvent, TaskCalendarSettings
from app.taskcalendar import service
from app.taskcalendar.graph import TaskCalendar
from tests.test_followups import stored, task

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
SOON = NOW + timedelta(days=2)


def person(email: str):
    return type("U", (), {"id": uuid.uuid4(), "email": email, "display_name": email.split("@")[0],
                          "entra_object_id": f"{email}-id"})()


SEBIN = person("sebin@hamdaz.com")
VISHNU = person("vishnu@hamdaz.com")


class Outlook:
    def __init__(self):
        self.events: dict[str, tuple[str, dict]] = {}
        self.calls: list[str] = []

    async def create(self, owner, body):
        event_id = f"ev{len(self.events) + 1}"
        self.events[event_id] = (owner, body)
        self.calls.append(f"create {owner}")
        return event_id

    async def update(self, owner, event_id, body):
        self.events[event_id] = (owner, body)
        self.calls.append(f"update {owner}")

    async def delete(self, owner, event_id):
        self.events.pop(event_id, None)
        self.calls.append(f"delete {owner}")


class World:
    """The list, per person, and the database's event rows."""

    def __init__(self):
        self.tasks: dict[str, list] = {SEBIN.email: [], VISHNU.email: []}
        self.failing: set[str] = set()
        self.rows: list[TaskCalendarEvent] = []

    async def lookup_id_for(self, email):
        return email

    async def tasks_assigned_to(self, lookup, limit):
        if lookup in self.failing:
            raise RuntimeError("SharePoint down")
        return self.tasks[lookup]


class Session:
    def __init__(self, world: World):
        self.world = world

    def add(self, row):
        self.world.rows.append(row)

    async def delete(self, row):
        self.world.rows.remove(row)

    async def flush(self):
        pass

    async def scalars(self, query):
        return type("R", (), {"all": lambda s: list(self.world.rows)})()


def _sync(monkeypatch, world: World, outlook: Outlook, *, unconfirmed=frozenset(), now=NOW):
    row = TaskCalendarSettings(enabled=True, reminder_minutes=2880, only_emails=[], only_title_contains="")

    async def same(*a, **k):
        return row

    async def follow(*a, **k):
        return FollowupSettings(team_id=uuid.uuid4())

    async def people(*a, **k):
        return [SEBIN, VISHNU]

    async def placeholders(session, tasks):
        return set(unconfirmed)

    from app.bcd import service as bcd

    monkeypatch.setattr(service, "get_settings", same)
    monkeypatch.setattr(service.fu, "get_settings", follow)
    monkeypatch.setattr(service.fu, "watched_people", people)
    monkeypatch.setattr(bcd, "unconfirmed_ids", placeholders)
    return asyncio.run(service.sync(Session(world), sharepoint=world, calendar=outlook, now=now))


def test_an_open_task_gets_an_event_at_its_bcd_shown_free_with_the_reminder(monkeypatch) -> None:
    world, outlook = World(), Outlook()
    world.tasks[SEBIN.email] = [task(id="257", title="test 6", bid_closing_date=stored(SOON))]
    report = _sync(monkeypatch, world, outlook)
    assert report.created == 1
    (owner, body), = outlook.events.values()
    assert owner == "sebin@hamdaz.com-id" and body["subject"] == "BCD: test 6"
    assert body["showAs"] == "free" and body["reminderMinutesBeforeStart"] == 2880
    assert body["start"]["dateTime"] == SOON.strftime("%Y-%m-%dT%H:%M:%S") and body["start"]["timeZone"] == "UTC"
    # Nothing changed: nothing to do.
    assert _sync(monkeypatch, world, outlook).created == 0 and outlook.calls == ["create sebin@hamdaz.com-id"]


def test_a_moved_bcd_moves_the_event_and_a_reassignment_moves_calendars(monkeypatch) -> None:
    world, outlook = World(), Outlook()
    world.tasks[SEBIN.email] = [task(id="257", title="test 6", bid_closing_date=stored(SOON))]
    _sync(monkeypatch, world, outlook)
    world.tasks[SEBIN.email] = [task(id="257", title="test 6", bid_closing_date=stored(SOON + timedelta(days=1)))]
    assert _sync(monkeypatch, world, outlook).updated == 1
    world.tasks[SEBIN.email] = []
    world.tasks[VISHNU.email] = [task(id="257", title="test 6", bid_closing_date=stored(SOON))]
    assert _sync(monkeypatch, world, outlook).moved == 1
    (owner, _), = outlook.events.values()
    assert owner == "vishnu@hamdaz.com-id"


def test_a_submitted_task_loses_its_event(monkeypatch) -> None:
    world, outlook = World(), Outlook()
    world.tasks[SEBIN.email] = [task(id="257", title="test 6", bid_closing_date=stored(SOON))]
    _sync(monkeypatch, world, outlook)
    world.tasks[SEBIN.email] = [task(id="257", title="test 6", bid_closing_date=stored(SOON),
                                     submission_status="Submitted")]
    assert _sync(monkeypatch, world, outlook).removed == 1 and not outlook.events


def test_no_event_for_a_placeholder_bcd_or_one_already_past(monkeypatch) -> None:
    world, outlook = World(), Outlook()
    world.tasks[SEBIN.email] = [
        task(id="1", title="placeholder", bid_closing_date=stored(SOON)),
        task(id="2", title="past", bid_closing_date=stored(NOW - timedelta(hours=1))),
        task(id="3", title="no bcd", bid_closing_date=None, due_date=None),
    ]
    report = _sync(monkeypatch, world, outlook, unconfirmed={"1"})
    assert report.created == 0 and report.skipped_placeholder == 1


def test_a_failed_read_never_empties_a_calendar(monkeypatch) -> None:
    world, outlook = World(), Outlook()
    world.tasks[SEBIN.email] = [task(id="257", title="test 6", bid_closing_date=stored(SOON))]
    _sync(monkeypatch, world, outlook)
    world.failing.add(SEBIN.email)
    report = _sync(monkeypatch, world, outlook)
    assert report.removed == 0 and len(outlook.events) == 1 and report.errors


def test_the_event_body_is_a_free_half_hour_at_the_bcd() -> None:
    body = TaskCalendar.event_body(subject="BCD: x", starts=SOON, html="<p>x</p>", reminder_minutes=60)
    assert body["end"]["dateTime"] == (SOON + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S")
    assert body["isReminderOn"] is True and body["responseRequested"] is False
