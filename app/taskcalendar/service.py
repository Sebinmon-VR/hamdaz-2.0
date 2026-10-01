"""Keep each open task's BCD in its holder's Outlook calendar.

Every run reads the watched people's tasks and makes the calendars agree:

* an open task with a real BCD ahead gets an event at its BCD, in its
  holder's calendar, shown as *free*, with Outlook's reminder set ahead
  (two days by default);
* a moved BCD moves the event; a reassigned task's event moves to the new
  holder's calendar;
* a task submitted or completed, removed from the list, or outside a trial's
  narrowing has its event taken out.

Not given an event: a task with no BCD, one whose BCD has passed, and one
whose BCD is still the assignment-time placeholder (``app.bcd``) — a calendar
deadline at the wrong time is worse than none. Who is watched is the overdue
follow-up's team; this module's settings can only narrow it.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html import escape
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.followups import service as fu
from app.models.task_calendar import TaskCalendarEvent, TaskCalendarSettings
from app.models.user import User
from app.proposals.sharepoint import ProposalTask, SharePointProposals
from app.taskcalendar.graph import EventGone, TaskCalendar

logger = logging.getLogger("hamdaz.taskcalendar")


class CalendarSettingsError(Exception):
    pass


async def get_settings(session: AsyncSession) -> TaskCalendarSettings:
    row = await session.get(TaskCalendarSettings, 1)
    if row is None:
        row = TaskCalendarSettings(id=1, only_emails=[])
        session.add(row)
        await session.flush()
    return row


_EDITABLE: Final = frozenset({"enabled", "reminder_minutes", "only_emails", "only_title_contains"})


async def update_settings(
    session: AsyncSession, *, actor_id: uuid.UUID, changes: dict[str, Any]
) -> TaskCalendarSettings:
    row = await get_settings(session)
    unknown = set(changes) - _EDITABLE
    if unknown:
        raise CalendarSettingsError(f"Not a setting: {', '.join(sorted(unknown))}.")
    if "reminder_minutes" in changes:
        minutes = int(changes["reminder_minutes"])
        # Outlook takes up to four weeks ahead.
        if not 0 <= minutes <= 40320:
            raise CalendarSettingsError("The reminder can be up to four weeks before the BCD.")
        changes["reminder_minutes"] = minutes
    if "only_emails" in changes:
        changes["only_emails"] = fu._clean_emails(changes["only_emails"])
    if "only_title_contains" in changes:
        changes["only_title_contains"] = (changes["only_title_contains"] or "").strip()[:200]
    for key, value in changes.items():
        setattr(row, key, value)
    row.updated_by_id = actor_id
    await session.flush()
    return row


def is_done(task: ProposalTask) -> bool:
    return (task.status or "").strip().casefold() == "completed" or fu.is_submitted(
        task.submission_status
    )


def owner_of(user: User) -> str:
    return user.entra_object_id or user.email


def event_html(task: ProposalTask) -> str:
    from app.followups.mailer import _when

    rows = [("Task", task.title or "(untitled)"), ("Bid closing", _when(fu.due_of(task)))]
    if task.end_user:
        rows.append(("End user", task.end_user))
    if task.status:
        rows.append(("Status", task.status))
    cells = "".join(
        f"<tr><td style='padding:4px 10px 4px 0;color:#5f6b77'>{escape(k)}</td>"
        f"<td style='padding:4px 0'>{escape(v)}</td></tr>"
        for k, v in rows
    )
    link = (
        f"<p><a href='{escape(task.web_url)}'>Open the task in SharePoint</a></p>"
        if task.web_url
        else ""
    )
    return (
        f"<div style='font-family:Segoe UI,Arial,sans-serif;font-size:13px'>"
        f"<p>The bid closing time for this task. Kept in step with the Proposals list by the "
        f"Hamdaz ERP — it moves if the BCD moves and goes once the bid is submitted.</p>"
        f"<table>{cells}</table>{link}</div>"
    )


@dataclass(slots=True)
class SyncReport:
    ran: bool = False
    tasks_read: int = 0
    created: int = 0
    updated: int = 0
    moved: int = 0
    removed: int = 0
    skipped_placeholder: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ran": self.ran, "tasks_read": self.tasks_read, "created": self.created,
            "updated": self.updated, "moved": self.moved, "removed": self.removed,
            "skipped_placeholder": self.skipped_placeholder, "errors": list(self.errors),
        }


async def sync(
    session: AsyncSession,
    *,
    sharepoint: SharePointProposals,
    calendar: TaskCalendar,
    force: bool = False,
    now: datetime | None = None,
) -> SyncReport:
    """Make the calendars agree with the list. ``force`` runs while off."""
    from app.bcd import service as bcd

    now = now or datetime.now(UTC)
    row = await get_settings(session)
    report = SyncReport()
    if not (row.enabled or force):
        return report
    report.ran = True

    fs = await fu.get_settings(session)
    people = await fu.watched_people(session, fs)
    only = set(row.only_emails or [])
    people = [u for u in people if not only or (u.email or "").casefold() in only]
    gate = asyncio.Semaphore(4)

    async def fetch(user: User):
        async with gate:
            try:
                lookup = await sharepoint.lookup_id_for(user.email)
                if lookup is None:
                    return user, [], None
                return user, await sharepoint.tasks_assigned_to(lookup, limit=500), None
            except Exception as exc:  # noqa: BLE001 - one person's failure is theirs
                return user, None, f"{user.email}: {type(exc).__name__}: {exc}"

    fetched = await asyncio.gather(*(fetch(u) for u in people))
    read_ok = {user.id for user, _, error in fetched if not error}
    all_tasks = [t for _, tasks, error in fetched if not error for t in tasks or []]
    report.tasks_read = len(all_tasks)
    report.errors += [error for _, _, error in fetched if error]
    unconfirmed = await bcd.unconfirmed_ids(session, all_tasks)

    existing = {e.task_id: e for e in (await session.scalars(select(TaskCalendarEvent))).all()}
    word = (row.only_title_contains or "").strip().casefold()

    wanted: dict[str, tuple[User, ProposalTask, datetime]] = {}
    for user, tasks, error in fetched:
        if error:
            continue
        for task in tasks or []:
            if word and word not in (task.title or "").casefold():
                continue
            if is_done(task):
                continue
            if task.id in unconfirmed:
                report.skipped_placeholder += 1
                continue
            due = fu.due_of(task)
            if due is None or (due <= now and task.id not in existing):
                continue
            wanted[task.id] = (user, task, due)

    for task_id, (user, task, due) in wanted.items():
        event = existing.get(task_id)
        owner = owner_of(user)
        title = (task.title or "(untitled)")[:2000]
        body = TaskCalendar.event_body(
            subject=f"BCD: {title}", starts=due, html=event_html(task),
            reminder_minutes=row.reminder_minutes,
        )
        try:
            if event is None:
                event_id = await calendar.create(owner, body)
                session.add(TaskCalendarEvent(
                    task_id=task_id, task_title=title, user_id=user.id, user=user, owner=owner,
                    event_id=event_id, bcd_at=due, reminder_minutes=row.reminder_minutes,
                    synced_at=now,
                ))
                report.created += 1
            elif event.owner != owner:
                await calendar.delete(event.owner, event.event_id)
                event.event_id = await calendar.create(owner, body)
                event.owner, event.user_id, event.user = owner, user.id, user
                event.bcd_at, event.task_title = due, title
                event.reminder_minutes, event.synced_at, event.last_error = row.reminder_minutes, now, None
                report.moved += 1
            elif (
                event.bcd_at != due
                or event.task_title != title
                or event.reminder_minutes != row.reminder_minutes
            ):
                try:
                    await calendar.update(owner, event.event_id, body)
                except EventGone:
                    # Deleted from the calendar by hand: put it back.
                    event.event_id = await calendar.create(owner, body)
                event.bcd_at, event.task_title = due, title
                event.reminder_minutes, event.synced_at, event.last_error = row.reminder_minutes, now, None
                report.updated += 1
        except Exception as exc:  # noqa: BLE001 - one event's failure is its own
            message = f"{title[:60]}: {type(exc).__name__}: {exc}"
            report.errors.append(message)
            if event is not None:
                event.last_error = message[:2000]
            logger.warning("calendar event for task %s: %s", task_id, exc)

    # Events no longer wanted: the task is done, gone, back to a placeholder,
    # or outside the trial. Only for people whose tasks were read — a failed
    # read must not empty somebody's calendar.
    for task_id, event in existing.items():
        if task_id in wanted or event.user_id not in read_ok and event.user_id in {u.id for u in people}:
            continue
        try:
            await calendar.delete(event.owner, event.event_id)
            await session.delete(event)
            report.removed += 1
        except Exception as exc:  # noqa: BLE001
            event.last_error = f"{type(exc).__name__}: {exc}"[:2000]
            report.errors.append(f"{event.task_title[:60]}: {exc}")

    row.last_run_at = now
    row.last_error = "; ".join(report.errors)[:2000] if report.errors else None
    await session.flush()
    return report


async def events(session: AsyncSession, *, limit: int = 200) -> list[TaskCalendarEvent]:
    return list(
        (
            await session.scalars(
                select(TaskCalendarEvent).order_by(TaskCalendarEvent.bcd_at).limit(limit)
            )
        ).all()
    )
