"""The status reminder: before a task is due, ask its holder where it stands.

Once a day, at its ask time, the reminder reads the watched people's tasks on
the Proposals list and picks those due within ``days_before`` days that are
neither **Completed** nor **Submitted**. Each person gets one mail listing
theirs, and each task links to a form showing what the list says now —
Status, Submission Status, Remarks, Working notes — for them to update.

**The answer can write to the Proposals list**, the one thing this module
does there, and only when ``StatusReminderSettings.write_sharepoint`` is on
(it ships off). Off, the answer is kept here with exactly what it would have
written. Only the fields the person changed are written, and a field changed
on the list since the form was opened is refused rather than overwritten.

Who is watched — the team, whose mailbox asks, the testing address — is the
overdue follow-up's (``FollowupSettings``); this module's own filters can
only narrow it, for a trial.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.followups import service as fu
from app.models.followup import FollowupSettings
from app.models.notification import NotificationKind
from app.models.status_reminder import ReminderStatus, StatusReminder, StatusReminderSettings
from app.models.user import User
from app.notifications import service as notifications
from app.proposals.sharepoint import ProposalTask, SharePointProposals

logger = logging.getLogger("hamdaz.reminders")

#: Where the form lives in the frontend.
FORM_PATH: Final = "/reminders"

#: The four columns the form shows and may write, by their SharePoint names.
STATUS: Final = "Status"
SUBMISSION: Final = "SubmissionStatus"
REMARKS: Final = "Remarks"
WORKING_NOTES: Final = "WorkingNotes"
FIELDS: Final = (STATUS, SUBMISSION, REMARKS, WORKING_NOTES)
CHOICE_FIELDS: Final = (STATUS, SUBMISSION)

#: The list's choices as they were when this was written, for when they
#: cannot be read. The live list wins — the choices are the team's to edit.
DEFAULT_CHOICES: Final = {
    STATUS: ["Not Started", "In Progress", "Completed", "On Hold"],
    SUBMISSION: ["Submitted", "Not Submitted"],
}

#: A choice SharePoint added and nobody named — "Choice 5" — is not offered.
_UNNAMED_CHOICE = re.compile(r"^choice \d+$", re.IGNORECASE)


class ReminderError(Exception):
    """Something the caller can fix; its message is for them."""


class ReminderNotFound(ReminderError):
    pass


class ReminderForbidden(ReminderError):
    pass


# ── settings ───────────────────────────────────────────────────────────


async def get_settings(session: AsyncSession) -> StatusReminderSettings:
    row = await session.get(StatusReminderSettings, 1)
    if row is None:
        row = StatusReminderSettings(id=1, only_emails=[])
        session.add(row)
        await session.flush()
    return row


_EDITABLE: Final = frozenset(
    {"enabled", "ask_time", "days_before", "write_sharepoint", "only_emails", "only_title_contains"}
)


async def update_settings(
    session: AsyncSession, *, actor_id: uuid.UUID, changes: dict[str, Any]
) -> StatusReminderSettings:
    row = await get_settings(session)
    unknown = set(changes) - _EDITABLE
    if unknown:
        raise ReminderError(f"Not a setting: {', '.join(sorted(unknown))}.")
    if "ask_time" in changes:
        try:
            changes["ask_time"] = fu._clock(changes["ask_time"] or "", "The reminder time")
        except fu.FollowupError as exc:
            raise ReminderError(str(exc)) from exc
    if "days_before" in changes:
        days = int(changes["days_before"])
        if not 1 <= days <= 14:
            raise ReminderError("Remind between 1 and 14 days before the due date.")
        changes["days_before"] = days
    if "only_emails" in changes:
        changes["only_emails"] = fu._clean_emails(changes["only_emails"])
    if "only_title_contains" in changes:
        changes["only_title_contains"] = (changes["only_title_contains"] or "").strip()[:200]
    for key, value in changes.items():
        setattr(row, key, value)
    row.updated_by_id = actor_id
    await session.flush()
    return row


# ── the rule ───────────────────────────────────────────────────────────


def is_completed(status: str | None) -> bool:
    return (status or "").strip().casefold() == "completed"


def is_done(task: ProposalTask) -> bool:
    """Nothing to remind about: the work is Completed or the bid Submitted."""
    return is_completed(task.status) or fu.is_submitted(task.submission_status)


@dataclass(frozen=True, slots=True)
class Decision:
    ask: bool
    due_at: datetime | None
    #: Why not, in a few words. For the log and the tests.
    why: str


def decide(
    task: ProposalTask, *, now: datetime, days_before: int, title_contains: str = ""
) -> Decision:
    """Whether ``task`` is due within ``days_before`` days and still open."""
    due = fu.due_of(task)
    if due is None:
        return Decision(False, None, "no due date")
    wanted = title_contains.strip().casefold()
    if wanted and wanted not in (task.title or "").casefold():
        return Decision(False, due, "title outside the filter")
    if is_done(task):
        return Decision(False, due, "completed or submitted")
    if due <= now:
        # Past due is the overdue follow-up's to ask about.
        return Decision(False, due, "already due")
    if due > now + timedelta(days=days_before):
        return Decision(False, due, "not yet within the reminder window")
    return Decision(True, due, "due soon")


def ask_moment(fs: FollowupSettings, row: StatusReminderSettings, day: date) -> datetime:
    """``day``'s reminder time, as a UTC instant, in the follow-up's zone."""
    hour, minute = (int(x) for x in (row.ask_time or "10:00").split(":")[:2])
    return datetime.combine(day, time(hour, minute), tzinfo=fu._settings_zone(fs)).astimezone(UTC)


def is_due(fs: FollowupSettings, row: StatusReminderSettings, now: datetime) -> bool:
    """On, past today's reminder time, and today's reminders not sent.

    Late is still today: a server that was down at the time sends when it
    is back, rather than skipping the day.
    """
    if not row.enabled:
        return False
    today = fu.local_day(fs, now)
    return row.last_run_on != today and now >= ask_moment(fs, row, today)


async def watched_people(
    session: AsyncSession, fs: FollowupSettings, row: StatusReminderSettings
) -> list[User]:
    """The follow-up's people, narrowed to this module's named ones if any."""
    people = await fu.watched_people(session, fs)
    only = set(row.only_emails or [])
    return [u for u in people if not only or (u.email or "").casefold() in only]


def form_link(settings: Settings, reminder_id: uuid.UUID) -> str:
    base = (settings.followup_link_url or settings.frontend_url).rstrip("/")
    return f"{base}{FORM_PATH}/{reminder_id}"


# ── the daily run ──────────────────────────────────────────────────────


@dataclass(slots=True)
class RunReport:
    ran: bool = False
    people: int = 0
    tasks_read: int = 0
    asked: int = 0
    closed: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ran": self.ran,
            "people": self.people,
            "tasks_read": self.tasks_read,
            "asked": self.asked,
            "closed": self.closed,
            "errors": list(self.errors),
        }


async def run(
    session: AsyncSession,
    *,
    settings: Settings,
    sharepoint: SharePointProposals,
    mailer,
    force: bool = False,
    now: datetime | None = None,
) -> RunReport:
    """The day's reminders, if they are due (or ``force``). Reads the list;
    never writes it."""
    now = now or datetime.now(UTC)
    row = await get_settings(session)
    fs = await fu.get_settings(session)
    report = RunReport()
    if not (force or is_due(fs, row, now)):
        return report
    report.ran = True

    people = await watched_people(session, fs, row)
    report.people = len(people)
    gate = asyncio.Semaphore(4)

    async def fetch(user: User) -> tuple[User, list[ProposalTask] | None, str | None]:
        async with gate:
            try:
                lookup = await sharepoint.lookup_id_for(user.email)
                if lookup is None:
                    return user, [], None
                return user, await sharepoint.tasks_assigned_to(lookup, limit=500), None
            except Exception as exc:  # noqa: BLE001 - one person's failure is theirs
                return user, None, f"{user.email}: {type(exc).__name__}: {exc}"

    fetched = await asyncio.gather(*(fetch(u) for u in people))
    # A task whose BCD is still the assignment-time placeholder waits for the
    # real date, in every module alike. See ``app.bcd.service``.
    from app.bcd import service as bcd

    held = await bcd.held_ids(
        session, [t for _, tasks, error in fetched if not error for t in tasks or []]
    )
    ids = {t.id for _, tasks, error in fetched if not error for t in tasks or []}
    known: dict[str, list[StatusReminder]] = {}
    if ids:
        for existing in (
            await session.scalars(select(StatusReminder).where(StatusReminder.task_id.in_(ids)))
        ).all():
            known.setdefault(existing.task_id, []).append(existing)

    batches: dict[uuid.UUID, tuple[User, list[StatusReminder]]] = {}
    for user, tasks, error in fetched:
        if error:
            report.errors.append(error)
            continue
        report.tasks_read += len(tasks or [])
        for task in tasks or []:
            asked_before = known.get(task.id, [])
            report.closed += close_pending(
                [r for r in asked_before if r.is_open], task, now
            )
            if task.id in held:
                continue
            decision = decide(
                task, now=now, days_before=row.days_before, title_contains=row.only_title_contains
            )
            if not decision.ask or decision.due_at is None:
                continue
            if any(r.due_at == decision.due_at for r in asked_before):
                continue
            made = await _record(session, user, task, decision.due_at, team_id=fs.team_id)
            batches.setdefault(user.id, (user, []))[1].append(made)
            known.setdefault(task.id, []).append(made)
            report.asked += 1

    for user, made in batches.values():
        sender = await fu._sender_for(session, fs, user)
        await _send(made, sender=sender, settings=settings, mailer=mailer,
                    redirect_to=fs.test_mail_to, now=now)

    if not force and not report.errors:
        # A failed read leaves the day open, so the next poll tries again;
        # everybody already asked is not asked twice. "Run now" is somebody
        # trying it, and does not stand in for the day's reminders.
        row.last_run_on = fu.local_day(fs, now)
    row.last_run_at = now
    row.last_error = "; ".join(report.errors)[:2000] if report.errors else None
    await session.flush()
    return report


def close_pending(pending: list[StatusReminder], task: ProposalTask, now: datetime) -> int:
    """Close the unanswered reminders on ``task`` that no longer stand."""
    closed = 0
    due = fu.due_of(task)
    for row in pending:
        note = None
        if is_done(task):
            note = "The task was marked Completed or Submitted before anybody answered."
        elif due is not None and abs((due - row.due_at).total_seconds()) > 60:
            note = "The task's due date changed before anybody answered."
        elif row.due_at <= now:
            note = "The due time passed before anybody answered; the overdue follow-up asks from here."
        if note:
            row.status = ReminderStatus.CLOSED
            row.closed_note = note
            closed += 1
    return closed


async def _record(
    session: AsyncSession,
    user: User,
    task: ProposalTask,
    due_at: datetime,
    *,
    team_id: uuid.UUID | None,
) -> StatusReminder:
    """The reminder as a row, the task as it now is, and the in-app notice."""
    reminder = StatusReminder(
        task_id=task.id,
        task_title=(task.title or "(untitled)")[:2000],
        task_url=task.web_url,
        end_user=(task.end_user or None) and task.end_user[:300],
        due_at=due_at,
        team_id=team_id,
        assignee_id=user.id,
        assignee=user,
        assignee_email=user.email,
        status=ReminderStatus.PENDING,
        changes={},
    )
    _snapshot(reminder, task)
    session.add(reminder)
    await session.flush()
    await notifications.notify(
        session,
        users=[user],
        kind=NotificationKind.STATUS_REMINDER,
        title=f"Status update needed: {reminder.task_title[:200]}",
        body="This task is due soon. Check its status, submission status, remarks and "
        "working notes, and update them.",
        link=f"{FORM_PATH}/{reminder.id}",
        source="status-reminder",
        source_id=str(reminder.id),
        payload={"task_id": task.id, "due_at": due_at.isoformat()},
    )
    return reminder


def _snapshot(reminder: StatusReminder, task: ProposalTask) -> None:
    notes = fu.notes_of(task)
    reminder.status_at_ask = (task.status or None) and task.status[:80]
    reminder.submission_at_ask = (task.submission_status or None) and task.submission_status[:80]
    reminder.remarks_at_ask = notes.remarks or None
    reminder.working_notes_at_ask = notes.working_notes or None


async def _send(
    rows: list[StatusReminder],
    *,
    sender: User,
    settings: Settings,
    mailer,
    redirect_to: str | None,
    now: datetime,
) -> None:
    """One mail to one person, listing their reminders."""
    for r in rows:
        r.asked_from_email = sender.email
    if not settings.notify_by_email:
        for r in rows:
            r.ask_error = "Email is switched off for this deployment (NOTIFY_BY_EMAIL)."
        return
    try:
        await mailer.send_reminders(
            rows,
            sender=sender,
            links={r.id: form_link(settings, r.id) for r in rows},
            redirect_to=redirect_to,
        )
    except Exception as exc:  # noqa: BLE001 - the in-app notices still stand
        error = f"{type(exc).__name__}: {exc}"[:2000]
        for r in rows:
            r.ask_error = error
        logger.warning("status reminder to %s not sent: %s", rows[0].assignee_email, exc)
        return
    for r in rows:
        r.asked_at = now


async def ask_now(
    session: AsyncSession,
    *,
    user: User,
    task_id: str,
    settings: Settings,
    sharepoint: SharePointProposals,
    mailer,
) -> StatusReminder:
    """Remind ``user`` about one of their own tasks now, to try it out.

    The task must be assigned to them, so a test only ever mails the person
    running it — from their own mailbox. No team, so it is nobody else's to
    read. Asked again about the same deadline, the earlier trial is reopened
    and sent afresh. The list is only read.
    """
    try:
        task = await sharepoint.task(task_id)
    except Exception as exc:  # noqa: BLE001
        raise ReminderNotFound("That task could not be read from the Proposals list.") from exc
    lookup = await sharepoint.lookup_id_for(user.email)
    if lookup is None or task.assigned_to_lookup_id != lookup:
        raise ReminderForbidden("A test can only use a task assigned to you.")
    now = datetime.now(UTC)
    due = fu.due_of(task) or now
    row = await session.scalar(
        select(StatusReminder).where(StatusReminder.task_id == task.id, StatusReminder.due_at == due)
    )
    if row is not None and row.team_id is not None:
        raise ReminderError("This task already has a real reminder for its due date.")
    if row is None:
        row = await _record(session, user, task, due, team_id=None)
    else:
        row.status, row.answered_at, row.changes = ReminderStatus.PENDING, None, {}
        row.written_at = row.write_error = row.closed_note = row.ask_error = None
        _snapshot(row, task)
    await _send([row], sender=user, settings=settings, mailer=mailer, redirect_to=None, now=now)
    await session.flush()
    return row


# ── the form and the answer ────────────────────────────────────────────


async def get(session: AsyncSession, reminder_id: uuid.UUID) -> StatusReminder:
    row = await session.get(StatusReminder, reminder_id)
    if row is None:
        raise ReminderNotFound("There is no such reminder.")
    return row


async def may_see(session: AsyncSession, row: StatusReminder, *, user: User, roles: set[str]) -> bool:
    """The person asked, their team's managers and leads, and administrators."""
    if row.assignee_id == user.id:
        return True
    from app.proposals.oversight import GLOBAL_OVERSIGHT, TEAM_OVERSIGHT

    if roles & GLOBAL_OVERSIGHT:
        return True
    if row.team_id is None:
        return False
    from app.teams.service import team_role_keys

    held = await team_role_keys(session, team_id=row.team_id, user_id=user.id)
    return bool(held & TEAM_OVERSIGHT)


@dataclass(frozen=True, slots=True)
class LiveTask:
    """The four columns as the list has them now, as text."""

    values: dict[str, str]
    choices: dict[str, list[str]]


def live_values(task: ProposalTask) -> dict[str, str]:
    notes = fu.notes_of(task)
    return {
        STATUS: (task.status or "").strip(),
        SUBMISSION: (task.submission_status or "").strip(),
        REMARKS: notes.remarks,
        WORKING_NOTES: notes.working_notes,
    }


async def choices(sharepoint: SharePointProposals) -> dict[str, list[str]]:
    """Status and Submission Status choices from the list, minus unnamed ones."""
    out = {k: list(v) for k, v in DEFAULT_CHOICES.items()}
    try:
        for column in await sharepoint.list_columns():
            if column.get("name") in CHOICE_FIELDS and column.get("choices"):
                named = [c for c in column["choices"] if not _UNNAMED_CHOICE.match(c.strip())]
                if named:
                    out[column["name"]] = named
    except Exception as exc:  # noqa: BLE001 - the defaults stand in
        logger.warning("list choices not read: %s", exc)
    return out


async def live(sharepoint: SharePointProposals, task_id: str) -> LiveTask:
    try:
        task = await sharepoint.task(task_id)
    except Exception as exc:  # noqa: BLE001
        raise ReminderError(
            "The task could not be read from the Proposals list just now. Try again in a minute."
        ) from exc
    return LiveTask(live_values(task), await choices(sharepoint))


def _require_open_and_theirs(row: StatusReminder, user: User) -> None:
    if row.assignee_id != user.id:
        raise ReminderForbidden("Only the person this reminder was sent to can answer it.")
    if not row.is_open:
        raise ReminderError("This reminder is already closed.")


def changes_for(
    wanted: dict[str, str], seen: dict[str, str], current: LiveTask
) -> dict[str, str]:
    """What the answer changes, checked against the list as it is now.

    A field left as the form showed it is not written. A field the person
    changed that somebody else changed on the list since the form opened is
    refused — writing it would silently undo their change.
    """
    out: dict[str, str] = {}
    for name in FIELDS:
        if name not in wanted:
            continue
        value = (wanted[name] or "").strip()
        before = current.values.get(name, "")
        if value == before:
            continue
        shown = seen.get(name)
        if shown is not None and value == shown.strip():
            # Left as the form showed it: not theirs to write, even if the
            # list has moved on since.
            continue
        if shown is not None and shown.strip() != before:
            label = {WORKING_NOTES: "Working notes", SUBMISSION: "Submission Status"}.get(name, name)
            raise ReminderError(
                f"{label} was changed on the Proposals list after you opened this form. "
                f"Reload the page to see it, then make your change again."
            )
        if name in CHOICE_FIELDS:
            if not value:
                raise ReminderError(f"Choose a {'Status' if name == STATUS else 'Submission Status'}.")
            if value not in current.choices.get(name, []):
                raise ReminderError(f"“{value}” is not one of the list's choices.")
        if len(value) > 10_000:
            raise ReminderError("Keep the notes under 10,000 characters.")
        out[name] = value
    return out


async def answer(
    session: AsyncSession,
    row: StatusReminder,
    *,
    user: User,
    wanted: dict[str, str],
    seen: dict[str, str],
    sharepoint: SharePointProposals,
    settings: Settings | None = None,
    mailer=None,
) -> StatusReminder:
    """File the answer, write what changed to the list when that is on, and
    report it to the team's managers straight away (see :func:`_report`)."""
    _require_open_and_theirs(row, user)
    current = await live(sharepoint, row.task_id)
    changes = changes_for(wanted, seen, current)
    now = datetime.now(UTC)
    row.changes = changes
    row.status = ReminderStatus.ANSWERED
    row.answered_at = now
    settings_row = await get_settings(session)
    if changes and settings_row.write_sharepoint:
        try:
            await sharepoint.update_task(row.task_id, changes)
            row.written_at = now
            row.write_error = None
        except Exception as exc:  # noqa: BLE001 - the answer is kept regardless
            row.write_error = f"{type(exc).__name__}: {exc}"[:2000]
            logger.warning("reminder %s not written to the list: %s", row.id, exc)
    await session.flush()
    if settings is not None and mailer is not None:
        await _report(
            session, row, user, before=current.values, settings=settings, mailer=mailer,
            writes=settings_row.write_sharepoint,
        )
    return row


async def _report(
    session: AsyncSession,
    row: StatusReminder,
    user: User,
    *,
    before: dict[str, str],
    settings: Settings,
    mailer,
    writes: bool,
) -> None:
    """The answer, mailed at once from the person to their team's managers.

    Never the CEO. A trial — a reminder with no team, made by "Try it" — goes
    from and to the person who answered, marked TEST and naming the managers
    it would have reached, so trying it out mails nobody else. The follow-up's
    testing address, when set, catches the real ones the same way. A mail that
    fails is logged; the answer stands regardless.
    """
    if not settings.notify_by_email:
        return
    fs = await fu.get_settings(session)
    trial = row.team_id is None
    team_id = fs.team_id if trial else row.team_id
    mine = (user.email or "").casefold()
    to: list[str] = []
    for m in await fu.managers_of(session, team_id):
        address = (m.email or "").strip().lower()
        if address and address != mine and address not in to:
            to.append(address)
    redirect = (user.email or None) if trial else fs.test_mail_to
    if not to:
        if not trial:
            logger.info("reminder %s answered; the team has no manager to tell", row.id)
            return
        to = [mine]
    try:
        await mailer.send_update(
            row,
            sender=user,
            recipients=to,
            before=before,
            link=form_link(settings, row.id),
            writes=writes,
            redirect_to=redirect,
        )
    except Exception as exc:  # noqa: BLE001 - the answer is kept regardless
        logger.warning("status update %s not mailed to the managers: %s", row.id, exc)


# ── listings ───────────────────────────────────────────────────────────


async def mine(session: AsyncSession, user_id: uuid.UUID, *, limit: int = 100) -> list[StatusReminder]:
    return list(
        (
            await session.scalars(
                select(StatusReminder)
                .where(StatusReminder.assignee_id == user_id)
                .order_by(StatusReminder.created_at.desc())
                .limit(limit)
            )
        ).all()
    )


async def recent(session: AsyncSession, *, limit: int = 50) -> list[StatusReminder]:
    return list(
        (
            await session.scalars(
                select(StatusReminder).order_by(StatusReminder.updated_at.desc()).limit(limit)
            )
        ).all()
    )
