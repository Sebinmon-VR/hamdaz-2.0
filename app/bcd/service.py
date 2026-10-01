"""BCD confirmation: hold a task whose BCD is a placeholder, and get it fixed.

**The placeholder.** The flow that creates Proposals rows must fill the BCD and
fills it with the moment of assignment. On the list that reads as the row's
own ``Created`` time — or that time plus four hours, the UAE clock written as
if it were UTC — to the second. A BCD a person typed from Ariba sits on a
round time days away. So: a BCD within two minutes of ``Created`` or of
``Created + 4h`` is the placeholder (:func:`placeholder_of`).

**The hold.** While switched on, a task created since switch-on whose BCD is
still the placeholder, and that nobody has confirmed, is *held*: the overdue
follow-up does not ask why it is late, and the status reminder does not
remind. They ask :func:`held_ids`. The hold lifts the moment the BCD changes
on the list, or somebody confirms the date as it stands.

**The questions.** The assignee is asked, the team lead copied — only in
working hours (10:00–18:00 India time, Monday to Saturday by default); a task
that arrives after hours waits for the morning. Unanswered for two *working*
hours, it goes to the team's managers and approvers and the super admins, the
team lead still copied. Never the CEO.

**Nothing here writes to SharePoint.** The person corrects the BCD in the list
itself; the next read sees it and closes the check.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Final
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.followups import service as fu
from app.models.bcd_check import BcdCheck, BcdCheckSettings, BcdCheckStatus
from app.models.role import Role, UserRole
from app.models.team import TeamMembership
from app.models.user import User
from app.proposals.analytics import parse_when
from app.proposals.sharepoint import ProposalTask, SharePointProposals
from app.roles.catalogue import SUPER_ADMIN

logger = logging.getLogger("hamdaz.bcd")

#: Where the form lives in the frontend.
FORM_PATH: Final = "/bcd"

#: How the flow's placeholder sits against the row's Created time: the same
#: instant, the UAE clock written as UTC (+4h — what the list shows today), or
#: the India clock written as UTC (+5:30, should the flow ever run on India
#: time). These are real instants on both sides, so where the reader sits —
#: the UAE, India, the site's US Pacific — does not move them.
PLACEHOLDER_OFFSETS: Final = (timedelta(0), timedelta(hours=4), timedelta(hours=5, minutes=30))
PLACEHOLDER_TOLERANCE: Final = timedelta(minutes=2)

TEAM_LEAD_ROLES: Final = frozenset({"team_lead"})


class BcdError(Exception):
    """Something the caller can fix; its message is for them."""


class BcdNotFound(BcdError):
    pass


class BcdForbidden(BcdError):
    pass


# ── the placeholder ───────────────────────────────────────────────────


def placeholder_of(task: ProposalTask) -> datetime | None:
    """The task's BCD when it is the assignment-time placeholder, else None."""
    created = parse_when(task.created_at)
    bcd = parse_when(task.bid_closing_date)
    if created is None or bcd is None:
        return None
    for offset in PLACEHOLDER_OFFSETS:
        if abs(bcd - (created + offset)) <= PLACEHOLDER_TOLERANCE:
            return bcd
    return None


def is_done(task: ProposalTask) -> bool:
    return (task.status or "").strip().casefold() == "completed" or fu.is_submitted(
        task.submission_status
    )


# ── working time ──────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class WorkingTime:
    start: time
    end: time
    days: frozenset[int]
    zone: ZoneInfo

    def window(self, day: date) -> tuple[datetime, datetime] | None:
        """That day's working hours as UTC instants, or None on a day off."""
        if day.weekday() not in self.days:
            return None
        return (
            datetime.combine(day, self.start, tzinfo=self.zone).astimezone(UTC),
            datetime.combine(day, self.end, tzinfo=self.zone).astimezone(UTC),
        )

    def is_working(self, moment: datetime) -> bool:
        span = self.window(moment.astimezone(self.zone).date())
        return span is not None and span[0] <= moment < span[1]

    def next_start(self, moment: datetime) -> datetime:
        """``moment`` if it is working time, else the next working morning."""
        if self.is_working(moment):
            return moment
        day = moment.astimezone(self.zone).date()
        for offset in range(0, 15):
            span = self.window(day + timedelta(days=offset))
            if span is not None and span[0] >= moment:
                return span[0]
        return moment  # no working day configured: never wait forever

    def minutes_between(self, since: datetime, until: datetime) -> float:
        """Working minutes from ``since`` to ``until``: evenings, nights and
        days off do not count."""
        if until <= since:
            return 0.0
        total = 0.0
        day = since.astimezone(self.zone).date()
        last = until.astimezone(self.zone).date()
        while day <= last:
            span = self.window(day)
            if span is not None:
                start, end = max(span[0], since), min(span[1], until)
                if end > start:
                    total += (end - start).total_seconds() / 60
            day += timedelta(days=1)
        return total


def _clock(value: str, what: str) -> time:
    try:
        hour, minute = (int(x) for x in value.strip().split(":")[:2])
        return time(hour, minute)
    except (ValueError, TypeError, AttributeError) as exc:
        raise BcdError(f"{what} must be a time like 10:00.") from exc


def working_time(row: BcdCheckSettings) -> WorkingTime:
    try:
        zone = ZoneInfo(row.timezone or "Asia/Kolkata")
    except Exception:  # noqa: BLE001 - a bad name falls back
        zone = ZoneInfo("Asia/Kolkata")
    return WorkingTime(
        start=_clock(row.work_start or "10:00", "The start of the working day"),
        end=_clock(row.work_end or "18:00", "The end of the working day"),
        days=frozenset(row.work_days or []),
        zone=zone,
    )


# ── settings ──────────────────────────────────────────────────────────


async def get_settings(session: AsyncSession) -> BcdCheckSettings:
    row = await session.get(BcdCheckSettings, 1)
    if row is None:
        row = BcdCheckSettings(id=1, only_emails=[], work_days=[0, 1, 2, 3, 4, 5])
        session.add(row)
        await session.flush()
    return row


_EDITABLE: Final = frozenset(
    {"enabled", "work_start", "work_end", "timezone", "work_days", "escalate_after_minutes",
     "only_emails", "only_title_contains"}
)


async def update_settings(
    session: AsyncSession, *, actor_id: uuid.UUID, changes: dict[str, Any]
) -> BcdCheckSettings:
    row = await get_settings(session)
    unknown = set(changes) - _EDITABLE
    if unknown:
        raise BcdError(f"Not a setting: {', '.join(sorted(unknown))}.")
    for key in ("work_start", "work_end"):
        if key in changes:
            changes[key] = _clock(changes[key] or "", "A working-hours time").strftime("%H:%M")
    start = _clock(changes.get("work_start", row.work_start), "The start")
    end = _clock(changes.get("work_end", row.work_end), "The end")
    if end <= start:
        raise BcdError("The working day has to end after it starts.")
    if "timezone" in changes:
        try:
            ZoneInfo(changes["timezone"])
        except Exception as exc:  # noqa: BLE001
            raise BcdError(f"{changes['timezone']!r} is not a time zone.") from exc
    if "work_days" in changes:
        days = sorted({int(d) for d in changes["work_days"] or []})
        if not days or any(d < 0 or d > 6 for d in days):
            raise BcdError("Choose at least one working day (Monday = 0 … Sunday = 6).")
        changes["work_days"] = days
    if "escalate_after_minutes" in changes:
        minutes = int(changes["escalate_after_minutes"])
        if not 1 <= minutes <= 60 * 24 * 7:
            raise BcdError("Escalate after between 1 minute and a week of working time.")
        changes["escalate_after_minutes"] = minutes
    if "only_emails" in changes:
        changes["only_emails"] = fu._clean_emails(changes["only_emails"])
    if "only_title_contains" in changes:
        changes["only_title_contains"] = (changes["only_title_contains"] or "").strip()[:200]
    turning_on = changes.get("enabled") is True and not row.enabled
    for key, value in changes.items():
        setattr(row, key, value)
    if turning_on:
        # From now: switching it on does not send the backlog.
        row.watch_from = datetime.now(UTC)
    row.updated_by_id = actor_id
    await session.flush()
    return row


# ── the hold, for the other modules ───────────────────────────────────


async def held_ids(session: AsyncSession, tasks: list[ProposalTask]) -> set[str]:
    """The tasks every module waits on: a placeholder BCD, created since this
    was switched on, not confirmed. Empty while it is off.

    Fails safe: if the BCD tables are not there yet (code deployed before its
    migration), nothing is held and the caller — the live overdue follow-up —
    carries on as it did before, rather than failing with it.
    """
    try:
        async with session.begin_nested():
            row = await session.get(BcdCheckSettings, 1)
    except Exception as exc:  # noqa: BLE001 - never take the follow-up down
        logger.warning("BCD check unavailable, holding nothing: %s", exc)
        return set()
    if row is None or not row.enabled or row.watch_from is None:
        return set()
    candidates = {
        t.id
        for t in tasks
        if placeholder_of(t) is not None
        and (parse_when(t.created_at) or row.watch_from) >= row.watch_from
    }
    if not candidates:
        return set()
    confirmed = set(
        (
            await session.scalars(
                select(BcdCheck.task_id).where(
                    BcdCheck.task_id.in_(candidates),
                    BcdCheck.status == BcdCheckStatus.CONFIRMED,
                )
            )
        ).all()
    )
    return candidates - confirmed


async def unconfirmed_ids(session: AsyncSession, tasks: list[ProposalTask]) -> set[str]:
    """Tasks whose BCD is the placeholder and nobody has confirmed it —
    whether or not the check is switched on. What a calendar must not put a
    deadline in for: the date is the time of assignment, not the closing."""
    candidates = {t.id for t in tasks if placeholder_of(t) is not None}
    if not candidates:
        return set()
    try:
        async with session.begin_nested():
            confirmed = set(
                (
                    await session.scalars(
                        select(BcdCheck.task_id).where(
                            BcdCheck.task_id.in_(candidates),
                            BcdCheck.status == BcdCheckStatus.CONFIRMED,
                        )
                    )
                ).all()
            )
    except Exception:  # noqa: BLE001 - no table yet: nothing is confirmed
        confirmed = set()
    return candidates - confirmed


# ── who hears ─────────────────────────────────────────────────────────


async def _team_role_emails(
    session: AsyncSession, team_id: uuid.UUID | None, roles: frozenset[str]
) -> list[str]:
    if team_id is None:
        return []
    rows = await session.scalars(
        select(User)
        .join(TeamMembership, TeamMembership.user_id == User.id)
        .join(Role, Role.id == TeamMembership.role_id)
        .where(TeamMembership.team_id == team_id, Role.key.in_(roles), User.is_active.is_(True))
    )
    ceo = await fu.ceo_emails(session)
    return [a for a in _unique(u.email for u in rows.all()) if a not in ceo]


async def team_leads(session: AsyncSession, team_id: uuid.UUID | None) -> list[str]:
    return await _team_role_emails(session, team_id, TEAM_LEAD_ROLES)


async def escalation_list(session: AsyncSession, team_id: uuid.UUID | None) -> list[str]:
    """The team's managers and approvers, and the super admins. Never the CEO."""
    managers = [m.email for m in await fu.managers_of(session, team_id)]
    supers = (
        await session.scalars(
            select(User)
            .join(UserRole, UserRole.user_id == User.id)
            .join(Role, Role.id == UserRole.role_id)
            .where(Role.key == SUPER_ADMIN, User.is_active.is_(True))
        )
    ).all()
    ceo = await fu.ceo_emails(session)
    return [a for a in _unique([*managers, *(u.email for u in supers)]) if a not in ceo]


def _unique(addresses) -> list[str]:
    out: list[str] = []
    for address in addresses:
        clean = (address or "").strip().lower()
        if clean and clean not in out:
            out.append(clean)
    return out


def form_link(settings: Settings, check_id: uuid.UUID) -> str:
    base = (settings.followup_link_url or settings.frontend_url).rstrip("/")
    return f"{base}{FORM_PATH}/{check_id}"


def edit_link(task_url: str | None) -> str | None:
    """The task's own SharePoint edit form, where the BCD is corrected."""
    if not task_url:
        return None
    return task_url.replace("DispForm.aspx", "EditForm.aspx")


# ── the run ───────────────────────────────────────────────────────────


@dataclass(slots=True)
class RunReport:
    ran: bool = False
    tasks_read: int = 0
    found: int = 0
    asked: int = 0
    escalated: int = 0
    resolved: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ran": self.ran, "tasks_read": self.tasks_read, "found": self.found,
            "asked": self.asked, "escalated": self.escalated, "resolved": self.resolved,
            "errors": list(self.errors),
        }


def still_unset(check: BcdCheck, task: ProposalTask) -> bool:
    """Whether the task's BCD is still the one the check was opened on.

    A real check asks whether the BCD is still the placeholder. A trial (no
    team) may be opened on a task with a real BCD, so it asks whether the BCD
    is still what it was when the trial began — changing it in SharePoint is
    then how a tester sees the check close itself."""
    if check.team_id is None:
        return parse_when(task.bid_closing_date) == check.placeholder_bcd
    return placeholder_of(task) is not None


def resolve_against(check: BcdCheck, task: ProposalTask | None, now: datetime) -> bool:
    """Close an open check whose task no longer needs it. True if it closed."""
    if task is None:
        check.status, check.resolved_note = BcdCheckStatus.CLOSED, "The task is no longer on the list."
    elif not still_unset(check, task):
        check.status = BcdCheckStatus.CORRECTED
        check.resolved_note = f"BCD corrected on the list to {task.bid_closing_date}."
    elif is_done(task):
        check.status = BcdCheckStatus.CLOSED
        check.resolved_note = "The task was marked Completed or Submitted."
    else:
        return False
    check.resolved_at = now
    return True


async def run(
    session: AsyncSession,
    *,
    settings: Settings,
    sharepoint: SharePointProposals,
    mailer,
    force: bool = False,
    now: datetime | None = None,
) -> RunReport:
    """Find new placeholders, close the corrected, ask and escalate in
    working hours. ``force`` (Run now) runs while off and outside hours."""
    now = now or datetime.now(UTC)
    row = await get_settings(session)
    report = RunReport()
    if not (row.enabled or force):
        return report
    report.ran = True
    fs = await fu.get_settings(session)
    wt = working_time(row)

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
    seen: dict[str, ProposalTask] = {}
    for _, tasks, error in fetched:
        if error:
            report.errors.append(error)
        for t in tasks or []:
            seen[t.id] = t
    report.tasks_read = len(seen)

    open_checks = list(
        (await session.scalars(select(BcdCheck).where(BcdCheck.status == BcdCheckStatus.PENDING))).all()
    )
    known = set(
        (
            await session.scalars(select(BcdCheck.task_id).where(BcdCheck.task_id.in_(list(seen) or [""])))
        ).all()
    )

    # Close what has been dealt with — reading a task this run did not see.
    for check in open_checks:
        task = seen.get(check.task_id)
        if task is None:
            try:
                task = await sharepoint.task(check.task_id)
            except Exception:  # noqa: BLE001 - gone, or the list is down: try later
                continue
        if resolve_against(check, task, now):
            report.resolved += 1

    # New placeholders, from switch-on.
    word = (row.only_title_contains or "").strip().casefold()
    for user, tasks, _error in fetched:
        for task in tasks or []:
            if task.id in known or placeholder_of(task) is None or is_done(task):
                continue
            created = parse_when(task.created_at)
            if row.watch_from is None or created is None or created < row.watch_from:
                continue
            if word and word not in (task.title or "").casefold():
                continue
            session.add(_new_check(task, user, team_id=fs.team_id))
            known.add(task.id)
            report.found += 1
    await session.flush()

    if force or wt.is_working(now):
        pending = list(
            (await session.scalars(select(BcdCheck).where(BcdCheck.status == BcdCheckStatus.PENDING))).all()
        )
        report.asked += await _ask(session, [c for c in pending if c.asked_at is None],
                                   fs=fs, settings=settings, mailer=mailer, now=now)
        due = [
            c for c in pending
            if c.asked_at is not None and c.escalated_at is None
            and wt.minutes_between(c.asked_at, now) >= row.escalate_after_minutes
        ]
        report.escalated += await _escalate(session, due, fs=fs, settings=settings,
                                            mailer=mailer, now=now)

    row.last_run_at = now
    row.last_error = "; ".join(report.errors)[:2000] if report.errors else None
    await session.flush()
    return report


def _new_check(task: ProposalTask, user: User, *, team_id: uuid.UUID | None) -> BcdCheck:
    return BcdCheck(
        task_id=task.id,
        task_title=(task.title or "(untitled)")[:2000],
        task_url=task.web_url,
        task_created_at=parse_when(task.created_at),
        placeholder_bcd=placeholder_of(task),
        team_id=team_id,
        assignee_id=user.id,
        assignee=user,
        assignee_email=user.email,
        status=BcdCheckStatus.PENDING,
    )


async def _ask(session, checks: list[BcdCheck], *, fs, settings, mailer, now) -> int:
    """One mail per person, their team lead copied. A trial (no team) goes
    from and to the person only, naming whom it would have reached."""
    if not checks or not settings.notify_by_email:
        return 0
    by_person: dict[tuple[uuid.UUID, bool], list[BcdCheck]] = {}
    for c in checks:
        by_person.setdefault((c.assignee_id, c.team_id is None), []).append(c)
    sent = 0
    for (_, trial), rows in by_person.items():
        person = rows[0].assignee
        team_id = fs.team_id if trial else rows[0].team_id
        cc = [a for a in await team_leads(session, team_id) if a != (person.email or "").lower()]
        sender = person if trial else await fu._sender_for(session, fs, person)
        try:
            await mailer.send_ask(
                rows, sender=sender, to=[person.email], cc=cc,
                links={c.id: form_link(settings, c.id) for c in rows},
                redirect_to=person.email if trial else fs.test_mail_to,
            )
        except Exception as exc:  # noqa: BLE001 - tried again on the next run
            for c in rows:
                c.ask_error = f"{type(exc).__name__}: {exc}"[:2000]
            logger.warning("BCD question to %s not sent: %s", person.email, exc)
            continue
        for c in rows:
            c.asked_at, c.ask_error = now, None
        sent += len(rows)
    return sent


async def _escalate(session, checks: list[BcdCheck], *, fs, settings, mailer, now) -> int:
    """One mail per team to its managers, approvers and the super admins, the
    team lead copied. A trial goes from and to its person only."""
    if not checks or not settings.notify_by_email:
        return 0
    groups: dict[tuple[uuid.UUID | None, uuid.UUID | None], list[BcdCheck]] = {}
    for c in checks:
        # Trials are their own group per person: each goes only to its tester.
        key = (None, c.assignee_id) if c.team_id is None else (c.team_id, None)
        groups.setdefault(key, []).append(c)
    sent = 0
    for (team_id, tester), rows in groups.items():
        real_team = fs.team_id if tester else team_id
        to = await escalation_list(session, real_team)
        cc = await team_leads(session, real_team)
        tester_email = rows[0].assignee.email if tester else None
        if not to and not tester_email:
            for c in rows:
                c.escalate_error = "Nobody to escalate to: no manager, approver or super admin."
            continue
        sender_email = tester_email or (fs.digest_sender_email or to[0])
        try:
            await mailer.send_escalation(
                rows, sender_email=sender_email, to=to or [tester_email], cc=cc,
                links={c.id: form_link(settings, c.id) for c in rows},
                redirect_to=tester_email or fs.test_mail_to,
            )
        except Exception as exc:  # noqa: BLE001 - tried again on the next run
            for c in rows:
                c.escalate_error = f"{type(exc).__name__}: {exc}"[:2000]
            logger.warning("BCD escalation not sent: %s", exc)
            continue
        for c in rows:
            c.escalated_at, c.escalate_error = now, None
        sent += len(rows)
    return sent


# ── the form ──────────────────────────────────────────────────────────


async def get(session: AsyncSession, check_id: uuid.UUID) -> BcdCheck:
    row = await session.get(BcdCheck, check_id)
    if row is None:
        raise BcdNotFound("There is no such BCD check.")
    return row


async def may_act(session: AsyncSession, row: BcdCheck, *, user: User, roles: set[str]) -> bool:
    """The assignee, the team's leads, managers and approvers, and admins."""
    if row.assignee_id == user.id:
        return True
    from app.proposals.oversight import GLOBAL_OVERSIGHT

    if roles & GLOBAL_OVERSIGHT:
        return True
    if row.team_id is None:
        return False
    from app.teams.service import team_role_keys

    held = await team_role_keys(session, team_id=row.team_id, user_id=user.id)
    return bool(held & {"team_lead", "team_manager", "approver"})


async def confirm(session: AsyncSession, row: BcdCheck, *, user: User, now: datetime | None = None) -> BcdCheck:
    """The date as it stands is right: lift the hold."""
    if not row.is_open:
        raise BcdError("This BCD check is already closed.")
    row.status = BcdCheckStatus.CONFIRMED
    row.resolved_at = now or datetime.now(UTC)
    row.resolved_by_id = user.id
    row.resolved_by = user
    row.resolved_note = f"Confirmed as it stands by {user.display_name}."
    await session.flush()
    return row


async def try_on(
    session: AsyncSession,
    *,
    user: User,
    task_id: str,
    settings: Settings,
    sharepoint: SharePointProposals,
    mailer,
) -> BcdCheck:
    """The test path: a check on one of the caller's own tasks, asked at once,
    from and to the caller only — whatever its BCD. No team, so it is nobody
    else's to see. Again on the same task, the earlier trial starts over."""
    try:
        task = await sharepoint.task(task_id)
    except Exception as exc:  # noqa: BLE001
        raise BcdNotFound("That task could not be read from the Proposals list.") from exc
    lookup = await sharepoint.lookup_id_for(user.email)
    if lookup is None or task.assigned_to_lookup_id != lookup:
        raise BcdForbidden("A test can only use a task assigned to you.")
    row = await session.scalar(select(BcdCheck).where(BcdCheck.task_id == task.id))
    if row is not None and row.team_id is not None:
        raise BcdError("This task already has a real BCD check.")
    now = datetime.now(UTC)
    if row is None:
        row = _new_check(task, user, team_id=None)
        session.add(row)
    else:  # an earlier trial on the same task starts over
        row.status = BcdCheckStatus.PENDING
        row.asked_at = row.escalated_at = row.resolved_at = None
        row.ask_error = row.escalate_error = row.resolved_note = None
        row.resolved_by = None
    # The BCD as it is now: the trial closes when it changes.
    row.placeholder_bcd = parse_when(task.bid_closing_date)
    await session.flush()
    fs = await fu.get_settings(session)
    await _ask(session, [row], fs=fs, settings=settings, mailer=mailer, now=now)
    await session.flush()
    return row


async def open_checks(session: AsyncSession, *, limit: int = 200) -> list[BcdCheck]:
    return list(
        (
            await session.scalars(
                select(BcdCheck).order_by(BcdCheck.status != BcdCheckStatus.PENDING,
                                          BcdCheck.created_at.desc()).limit(limit)
            )
        ).all()
    )
