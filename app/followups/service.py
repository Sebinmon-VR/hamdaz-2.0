"""The overdue-task follow-up: who is asked, when, and what happens to the answer.

See ``app.models.followup`` for why this exists. This module holds the three
moving parts:

* :func:`decide` — the rule for one task, with no database, list or clock of
  its own, so it can be tested directly: is it past its due time by more than
  the grace, unfinished, inside the watch window, and in scope?
* :func:`sweep` — one pass over the watched people's tasks: ask about the ones
  that qualify, and close the pending asks whose task has since been finished
  or re-dated.
* :func:`answer` / :func:`mark_false_positive` — what the person does with the
  form, and the managers being told.

**Read-only over SharePoint.** The sweep reads the Proposals list and writes
nothing to it. Marking a false positive changes this app's record only; the
task's status is the person's to fix, in SharePoint, where the team works.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.followup import (
    FollowupSettings,
    FollowupStatus,
    TaskFollowup,
)
from app.models.notification import NotificationKind
from app.models.role import Role
from app.models.team import Team, TeamMembership
from app.models.user import User
from app.notifications import service as notifications
from app.proposals.analytics import parse_when
from app.proposals.sharepoint import ProposalTask, SharePointProposals

logger = logging.getLogger("hamdaz.followups")

#: Who in the team hears the reasons: its managers and its approvers. In
#: presales the approvers are the people who decide on the work (Sujeel as
#: well as Althaf), and they asked to hear why it is late. Not the leads. The
#: person answering is never sent their own reason.
MANAGER_ROLES: Final = frozenset({"team_manager", "approver"})

#: A team is asked about one member at a time, several at once — the same
#: courtesy to a shared tenant the team-tasks view extends.
MAX_CONCURRENT_FETCHES: Final = 6

#: Where the form lives, under the frontend.
FORM_PATH: Final = "/followups"

#: Days and deadlines are the UAE's, which keeps no daylight saving.
GULF: Final = timezone(timedelta(hours=4), "GST")


class FollowupError(Exception):
    """Something the caller asked for cannot be done. Safe to show a person."""


class FollowupNotFound(FollowupError):
    pass


class FollowupForbidden(FollowupError):
    pass


# ── settings ───────────────────────────────────────────────────────────


async def get_settings(session: AsyncSession) -> FollowupSettings:
    row = await session.get(FollowupSettings, 1)
    if row is None:
        row = FollowupSettings(id=1)
        session.add(row)
        await session.flush()
    return row


def _clean_emails(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for raw in values or []:
        value = str(raw).strip().lower()
        if value and "@" in value and value not in out:
            out.append(value)
    return out


async def update_settings(
    session: AsyncSession, *, actor_id: uuid.UUID, changes: dict[str, Any]
) -> FollowupSettings:
    """Only the keys given change. Switching it on starts the watch from now."""
    row = await get_settings(session)
    was_on = bool(row.enabled)

    if changes.get("enabled") is not None:
        row.enabled = bool(changes["enabled"])
    if changes.get("notify_managers_by_email") is not None:
        row.notify_managers_by_email = bool(changes["notify_managers_by_email"])
    if "team_id" in changes:
        team_id = changes["team_id"]
        if team_id is not None and await session.get(Team, team_id) is None:
            raise FollowupError("There is no such team.")
        row.team_id = team_id
    if "ask_from_user_id" in changes:
        sender = changes["ask_from_user_id"]
        if sender is not None and await session.get(User, sender) is None:
            raise FollowupError("There is no such person to send from.")
        row.ask_from_user_id = sender
    if "only_emails" in changes:
        row.only_emails = _clean_emails(changes["only_emails"])
    if changes.get("only_title_contains") is not None:
        row.only_title_contains = str(changes["only_title_contains"]).strip()[:120]
    if changes.get("grace_minutes") is not None:
        row.grace_minutes = int(changes["grace_minutes"])
    if changes.get("poll_seconds") is not None:
        row.poll_seconds = int(changes["poll_seconds"])

    # The end-of-day report.
    for key in ("digest_enabled", "digest_include_ceo"):
        if changes.get(key) is not None:
            setattr(row, key, bool(changes[key]))
    if changes.get("digest_time") is not None:
        row.digest_time = str(changes["digest_time"])
    if changes.get("digest_timezone") is not None:
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(str(changes["digest_timezone"]))
        except Exception as exc:  # noqa: BLE001
            raise FollowupError("That is not a timezone this server knows.") from exc
        row.digest_timezone = str(changes["digest_timezone"])
    if "digest_recipients" in changes:
        row.digest_recipients = _clean_emails(changes["digest_recipients"])
    if changes.get("digest_formats"):
        row.digest_formats = sorted({str(f) for f in changes["digest_formats"]})
    if "digest_sender_email" in changes:
        sender = (changes["digest_sender_email"] or "").strip().lower()
        row.digest_sender_email = sender or None

    # From the moment it is switched on, never from before. The archive of
    # bids that closed last year is not two hundred emails on the first tick.
    if row.enabled and not was_on:
        row.watch_from = datetime.now(UTC)
    if changes.get("watch_from") is not None:
        row.watch_from = changes["watch_from"]

    row.updated_by_id = actor_id
    await session.flush()
    return row


# ── the rule ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Decision:
    ask: bool
    #: The due date and time the task missed, when it has one.
    due_at: datetime | None
    #: Why not, in a few words. For the log and the tests.
    why: str


def due_of(task: ProposalTask) -> datetime | None:
    """The moment the task was due: the bid closing, else the end of the due day.

    **BCD leads.** It is the "BCD UAE Time" column, a real date *and time* —
    the moment the bid must be in — and submission is what the follow-up asks
    about. SharePoint hands it over in UTC; 13:51Z is 5:51 PM in the UAE.

    **DueDate is a date only**, with no time, so it is read as the end of that
    day in the UAE — 23:59:59 — rather than its midnight, which would have
    asked people about work on the morning it was due.
    """
    closing = parse_when(task.bid_closing_date)
    if closing is not None:
        return closing
    due = parse_when(task.due_date)
    if due is None:
        return None
    # A date-only column arrives as that day's midnight in the site's zone,
    # converted to UTC; read in UAE time it lands back on the right day.
    day = due.astimezone(GULF).date()
    return datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=GULF).astimezone(UTC)


def is_submitted(value: str | None) -> bool:
    """Whether a Submission Status says the bid went in.

    "Submitted" does; "Not Submitted" and a blank do not. Read by the words
    rather than against a fixed list, because the list's choices are the
    team's to edit and "Submitted to portal" means the same thing.
    """
    text = (value or "").strip().casefold()
    return "submitted" in text and not text.startswith("not") and "not submitted" not in text


def marked_not_submitted(value: str | None) -> bool:
    """Somebody has *set* the Submission Status to "Not Submitted".

    Different from a blank, which only means nobody has filled it in yet. An
    explicit "Not Submitted" is an answer — usually beside a Status of
    Completed — and it is asked about at once rather than at the deadline.
    """
    return "not submitted" in (value or "").strip().casefold()


def is_finished(task: ProposalTask) -> bool:
    """Done, for the follow-up: the bid was **submitted**.

    Submission Status decides, not Status. A task marked Completed whose
    submission still says "Not Submitted" is exactly the one worth asking
    about — the work may be written but the bid has not gone in, and a
    missed submission is the expensive kind of late.
    """
    return is_submitted(task.submission_status)


def decide(
    task: ProposalTask,
    *,
    now: datetime,
    grace_minutes: int,
    watch_from: datetime | None,
    title_contains: str = "",
) -> Decision:
    """Whether ``task`` should be asked about now."""
    due = due_of(task)
    if due is None:
        return Decision(False, None, "no due date")
    wanted = title_contains.strip().casefold()
    if wanted and wanted not in (task.title or "").casefold():
        return Decision(False, due, "title outside the filter")
    if is_finished(task):
        return Decision(False, due, "finished")
    if watch_from is None or due < watch_from:
        return Decision(False, due, "due before the watch began")
    # Marked "Not Submitted" outright: ask now, not at the deadline. The
    # countdown is for work still in hand; this one has been answered "no".
    if marked_not_submitted(task.submission_status):
        return Decision(True, due, "marked not submitted")
    if now < due + timedelta(minutes=max(0, grace_minutes)):
        return Decision(False, due, "grace not yet over")
    return Decision(True, due, "overdue")


# ── the sweep ──────────────────────────────────────────────────────────


@dataclass(slots=True)
class SweepReport:
    people: int = 0
    tasks_read: int = 0
    asked: int = 0
    resolved: int = 0
    errors: list[str] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "people": self.people,
            "tasks_read": self.tasks_read,
            "asked": self.asked,
            "resolved": self.resolved,
            "errors": list(self.errors or []),
        }


async def watched_people(session: AsyncSession, row: FollowupSettings) -> list[User]:
    """The team's members, narrowed to the named people when any are named."""
    if row.team_id is None:
        return []
    from app.teams import service as teams_service

    members = [user for user, _ in await teams_service.list_members(session, row.team_id)]
    only = set(row.only_emails or [])
    return [
        u for u in members
        if u.is_active and (not only or (u.email or "").casefold() in only)
    ]


async def managers_of(session: AsyncSession, team_id: uuid.UUID | None) -> list[User]:
    if team_id is None:
        return []
    rows = await session.scalars(
        select(User)
        .join(TeamMembership, TeamMembership.user_id == User.id)
        .join(Role, Role.id == TeamMembership.role_id)
        .where(TeamMembership.team_id == team_id, Role.key.in_(MANAGER_ROLES))
    )
    seen: dict[uuid.UUID, User] = {}
    for user in rows.all():
        if user.is_active:
            seen.setdefault(user.id, user)
    return list(seen.values())


def form_link(settings: Settings, followup_id: uuid.UUID) -> str:
    """The form, on the production app — see ``Settings.followup_link_url``."""
    base = (settings.followup_link_url or settings.frontend_url).rstrip("/")
    return f"{base}{FORM_PATH}/{followup_id}"


async def sweep(
    session: AsyncSession,
    *,
    settings: Settings,
    sharepoint: SharePointProposals,
    mailer,
    now: datetime | None = None,
    force: bool = False,
) -> SweepReport:
    """One pass: ask about what is newly overdue, close what has been dealt with.

    ``force`` runs it even when switched off — for the super admin's "run now"
    button, which is somebody asking explicitly.
    """
    row = await get_settings(session)
    report = SweepReport(errors=[])
    if not (row.enabled or force):
        return report
    now = now or datetime.now(UTC)
    people = await watched_people(session, row)
    report.people = len(people)
    if not people:
        row.last_run_at = now
        row.last_error = None if row.team_id else "No team is chosen, so nobody is watched."
        return report

    gate = asyncio.Semaphore(MAX_CONCURRENT_FETCHES)

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

    for user, tasks, error in fetched:
        if error:
            report.errors.append(error)
            continue
        report.tasks_read += len(tasks or [])
        for task in tasks or []:
            await _close_if_dealt_with(session, task, report)
            decision = decide(
                task,
                now=now,
                grace_minutes=row.grace_minutes,
                watch_from=row.watch_from,
                title_contains=row.only_title_contains,
            )
            if not decision.ask or decision.due_at is None:
                continue
            existing = await session.scalar(
                select(TaskFollowup.id).where(
                    TaskFollowup.task_id == task.id, TaskFollowup.due_at == decision.due_at
                )
            )
            if existing is not None:
                continue
            await _ask(
                session, row, user, task, decision.due_at,
                settings=settings, mailer=mailer, now=now,
            )
            report.asked += 1

    row.last_run_at = now
    row.last_error = "; ".join(report.errors)[:2000] if report.errors else None
    await session.flush()
    return report


async def _close_if_dealt_with(
    session: AsyncSession, task: ProposalTask, report: SweepReport
) -> None:
    """Resolve the pending asks on ``task`` that no longer stand."""
    pending = (
        await session.scalars(
            select(TaskFollowup).where(
                TaskFollowup.task_id == task.id,
                TaskFollowup.status == FollowupStatus.PENDING,
            )
        )
    ).all()
    if not pending:
        return
    due = due_of(task)
    for row in pending:
        note = None
        if is_finished(task):
            note = "The bid was marked submitted before anybody answered."
        elif due is not None and due != row.due_at:
            note = "The task's due date was moved before anybody answered."
        if note:
            row.status = FollowupStatus.RESOLVED
            row.resolved_note = note
            report.resolved += 1


async def _sender_for(session: AsyncSession, row: FollowupSettings, assignee: User) -> User:
    """Whose mailbox the ask goes out from: the configured sender, else the
    person themselves — so the mail always has a sender and is never sent from
    a colleague's mailbox that nobody chose."""
    if row.ask_from_user_id is not None:
        sender = await session.get(User, row.ask_from_user_id)
        if sender is not None and sender.is_active:
            return sender
    return assignee


async def _ask(
    session: AsyncSession,
    row: FollowupSettings,
    user: User,
    task: ProposalTask,
    due_at: datetime,
    *,
    settings: Settings,
    mailer,
    now: datetime,
) -> TaskFollowup:
    followup = TaskFollowup(
        task_id=task.id,
        task_title=(task.title or "(untitled)")[:2000],
        task_url=task.web_url,
        end_user=(task.end_user or None) and task.end_user[:300],
        status_at_ask=(task.submission_status or None) and task.submission_status[:80],
        due_at=due_at,
        task_modified_at=parse_when(task.modified_at),
        team_id=row.team_id,
        assignee_id=user.id,
        assignee=user,
        assignee_email=user.email,
        status=FollowupStatus.PENDING,
    )
    session.add(followup)
    await session.flush()

    link = form_link(settings, followup.id)
    early = due_at > now
    await notifications.notify(
        session,
        users=[user],
        kind=NotificationKind.TASK_OVERDUE,
        title=(
            f"Marked not submitted: {followup.task_title[:200]}"
            if early
            else f"Past its due date: {followup.task_title[:200]}"
        ),
        body=(
            "This bid is not marked submitted. Tell your manager why — or, if "
            "you have already updated it, mark this as a false positive."
        ),
        link=f"{FORM_PATH}/{followup.id}",
        source="followup",
        source_id=str(followup.id),
        payload={"task_id": task.id, "due_at": due_at.isoformat()},
    )

    sender = await _sender_for(session, row, user)
    followup.asked_from_email = sender.email
    if not settings.notify_by_email:
        followup.ask_error = "Email is switched off for this deployment (NOTIFY_BY_EMAIL)."
        return followup
    try:
        await mailer.send_ask(followup, sender=sender, link=link, early=early)
        followup.asked_at = now
    except Exception as exc:  # noqa: BLE001 - the in-app notice still stands
        followup.ask_error = f"{type(exc).__name__}: {exc}"[:2000]
        logger.warning("follow-up mail for task %s not sent: %s", task.id, exc)
    return followup


async def ask_now(
    session: AsyncSession,
    *,
    user: User,
    task_id: str,
    settings: Settings,
    sharepoint: SharePointProposals,
    mailer,
) -> TaskFollowup:
    """Ask ``user`` about one of their own tasks now, for trying the feature out.

    Skips the grace and the watch window — that is the point of it — but not
    ownership: the task must be assigned to the person asking, so a test can
    only ever mail the tester. The task is read from SharePoint, never written.
    A deadline already asked about returns the existing question rather than a
    second one.
    """
    try:
        task = await sharepoint.task(task_id)
    except Exception as exc:  # noqa: BLE001
        raise FollowupNotFound("That task could not be read from the Proposals list.") from exc
    lookup = await sharepoint.lookup_id_for(user.email)
    if lookup is None or task.assigned_to_lookup_id != lookup:
        raise FollowupForbidden("A test can only use a task assigned to you.")
    due = due_of(task) or datetime.now(UTC).replace(microsecond=0)
    existing = await session.scalar(
        select(TaskFollowup).where(TaskFollowup.task_id == task.id, TaskFollowup.due_at == due)
    )
    if existing is not None:
        return existing
    row = await get_settings(session)
    if row.team_id is None:
        raise FollowupError("Choose a team first — its managers are who the reason goes to.")
    return await _ask(
        session, row, user, task, due,
        settings=settings, mailer=mailer, now=datetime.now(UTC),
    )


# ── the person's answer ────────────────────────────────────────────────


async def get(session: AsyncSession, followup_id: uuid.UUID) -> TaskFollowup:
    row = await session.get(TaskFollowup, followup_id)
    if row is None:
        raise FollowupNotFound("There is no such follow-up.")
    return row


async def may_see(
    session: AsyncSession, row: TaskFollowup, *, user: User, roles: set[str]
) -> bool:
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


def _require_open_and_theirs(row: TaskFollowup, user: User) -> None:
    if row.assignee_id != user.id:
        raise FollowupForbidden("Only the person this was sent to can answer it.")
    if not row.is_open:
        raise FollowupError("This has already been answered or closed.")


async def answer(
    session: AsyncSession,
    row: TaskFollowup,
    *,
    user: User,
    reason: str,
    settings: Settings,
    followup_settings: FollowupSettings,
    mailer,
) -> TaskFollowup:
    """File the reason and tell the team's managers."""
    _require_open_and_theirs(row, user)
    text = (reason or "").strip()
    if len(text) < 3:
        raise FollowupError("Say a little more — the reason is what your manager reads.")
    now = datetime.now(UTC)
    row.reason = text[:5000]
    row.status = FollowupStatus.ANSWERED
    row.answered_at = now

    managers = [m for m in await managers_of(session, row.team_id) if m.id != user.id]
    if managers:
        await notifications.notify(
            session,
            users=managers,
            kind=NotificationKind.TASK_REASON,
            title=f"{user.display_name}: why “{row.task_title[:120]}” is late",
            body=text[:500],
            link=f"{FORM_PATH}/{row.id}",
            source="followup-reason",
            source_id=str(row.id),
            payload={"task_id": row.task_id},
        )
    await _forward(row, user, managers, settings, followup_settings, mailer, now)
    await session.flush()
    return row


async def mark_false_positive(
    session: AsyncSession, row: TaskFollowup, *, user: User, note: str | None
) -> TaskFollowup:
    """The person says the task was already dealt with. Recorded; nobody mailed."""
    _require_open_and_theirs(row, user)
    row.status = FollowupStatus.FALSE_POSITIVE
    row.answered_at = datetime.now(UTC)
    row.resolved_note = (note or "").strip()[:2000] or (
        "Marked as a false positive: the task had already been updated."
    )
    await session.flush()
    return row


async def _forward(
    row: TaskFollowup,
    user: User,
    managers: list[User],
    settings: Settings,
    followup_settings: FollowupSettings,
    mailer,
    now: datetime,
) -> None:
    if not managers:
        row.forward_error = "This team has no manager to send the reason to."
        return
    if not (settings.notify_by_email and followup_settings.notify_managers_by_email):
        row.forward_error = "Email to managers is switched off; they were notified in the app."
        return
    try:
        await mailer.send_reason(
            row,
            sender=user,
            recipients=[m.email for m in managers if m.email],
            link=form_link(settings, row.id),
        )
        row.forwarded_at = now
        row.forward_error = None
    except Exception as exc:  # noqa: BLE001 - the reason is filed regardless
        row.forward_error = f"{type(exc).__name__}: {exc}"[:2000]
        logger.warning("reason for follow-up %s not mailed: %s", row.id, exc)


# ── listings ───────────────────────────────────────────────────────────


async def mine(session: AsyncSession, user_id: uuid.UUID, *, limit: int = 100) -> list[TaskFollowup]:
    return list(
        (
            await session.scalars(
                select(TaskFollowup)
                .where(TaskFollowup.assignee_id == user_id)
                .order_by(TaskFollowup.created_at.desc())
                .limit(limit)
            )
        ).all()
    )


async def for_team(session: AsyncSession, team_id: uuid.UUID, *, limit: int = 200) -> list[TaskFollowup]:
    return list(
        (
            await session.scalars(
                select(TaskFollowup)
                .where(TaskFollowup.team_id == team_id)
                .order_by(TaskFollowup.created_at.desc())
                .limit(limit)
            )
        ).all()
    )


# ── due today ──────────────────────────────────────────────────────────


def today_bounds(now: datetime) -> tuple[datetime, datetime]:
    """Midnight to midnight, Gulf time, as UTC instants."""
    local = now.astimezone(GULF)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


def watches(row: FollowupSettings | None, user: User, task: ProposalTask) -> str | None:
    """Why the follow-up will *not* ask about this task, or None if it will.

    The same filters the sweep applies, said in words, so a screen can tell a
    task that is waiting for its question from one that will never get one.
    """
    if row is None or not row.enabled:
        return "The follow-up is switched off."
    only = set(row.only_emails or [])
    if only and (user.email or "").casefold() not in only:
        return "This person is outside the trial — only the people named in the settings are asked."
    word = (row.only_title_contains or "").strip().casefold()
    if word and word not in (task.title or "").casefold():
        return f"Outside the trial — only tasks with “{row.only_title_contains}” in the title are asked about."
    due = due_of(task)
    if row.watch_from is None or (due is not None and due < row.watch_from):
        return "Due before the follow-up was switched on."
    return None


def due_today_rows(
    people: list[tuple[User, list[ProposalTask]]],
    *,
    now: datetime,
    grace_minutes: int,
    asked: dict[tuple[str, datetime], TaskFollowup],
    settings_row: FollowupSettings | None = None,
) -> list[dict[str, Any]]:
    """Every task due today, finished or not, soonest first.

    By the same due rule the follow-up asks on, so the countdown on screen
    and the moment the question goes out are the same moment.
    """
    start, end = today_bounds(now)
    out: list[dict[str, Any]] = []
    for user, tasks in people:
        for task in tasks:
            due = due_of(task)
            if due is None or not (start <= due < end):
                continue
            followup = asked.get((task.id, due))
            finished = is_finished(task)
            # Asked now rather than at the deadline — see ``decide``.
            reason_now = not finished and marked_not_submitted(task.submission_status)
            out.append(
                {
                    "task_id": task.id,
                    "title": task.title,
                    "task_url": task.web_url,
                    "end_user": task.end_user,
                    "status": task.status,
                    "submission_status": task.submission_status,
                    "finished": finished,
                    "reason_now": reason_now,
                    "assignee_name": user.display_name,
                    "assignee_email": user.email,
                    "due_at": due,
                    "ask_at": now if reason_now else due + timedelta(minutes=max(0, grace_minutes)),
                    "followup_id": followup.id if followup else None,
                    "followup_status": followup.status if followup else None,
                    # Whether the mail reached them — the in-app notice and
                    # the banner stand either way.
                    "mailed": bool(followup and followup.asked_at),
                    "mail_error": followup.ask_error if followup else None,
                    "not_watched": watches(settings_row, user, task),
                }
            )
    out.sort(key=lambda r: (r["finished"], r["due_at"]))
    return out
