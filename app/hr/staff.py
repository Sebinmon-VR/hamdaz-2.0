"""The staff list, and what happens when somebody leaves.

HR records an exit once: resigned on, last working day, why. Everything else
follows from that row without anybody remembering to do it:

* **assignment** — the person carries the derived ``leaving`` label from the
  moment the row exists, and ``assignment.service.is_excluded`` refuses anyone
  holding it whatever their team's policy says. The stored rankings are
  rebuilt here straight away, because the next tender is chosen from them;
* **sign-in** — once the last working day has passed, :func:`end_access_for_leavers`
  switches the account off. The HR worker runs it through the day, and recording
  an exit whose last day is already behind runs it on the spot;
* **payroll** — a linked payroll record with no leaving date gets this one, so
  the final month is prorated and gratuity is flagged. Never overwritten if an
  accountant already typed one.

Withdrawing undoes exactly what this module did: the label goes with the row,
the account comes back on only if this module switched it off, and the payroll
date is cleared only if it is still the one written here.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.assignment import AssignmentPolicy
from app.models.employment import EmployeeExit, EmploymentEvent, ExitKind
from app.models.payroll import PayrollEmployee, PayrollEvent
from app.models.team import Team, TeamMembership
from app.models.user import User

logger = logging.getLogger("hamdaz.hr.staff")

#: Whose midnight ends a last working day. Dubai's comes after India's, so
#: nobody in either office loses the evening of their final day.
OFFICE_TZ = ZoneInfo("Asia/Dubai")

KIND_LABELS: dict[str, str] = {
    ExitKind.RESIGNED: "Resigned",
    ExitKind.TERMINATED: "Terminated",
    ExitKind.CONTRACT_ENDED: "Contract ended",
    ExitKind.OTHER: "Other",
}


class StaffError(Exception):
    """A staff operation was refused. Safe to show a user."""


def office_today() -> date:
    return datetime.now(OFFICE_TZ).date()


def status_of(user: User, exit_row: EmployeeExit | None, today: date) -> str:
    """``active``, ``notice``, ``left``, or ``inactive`` (switched off, no exit
    recorded — a service account, or somebody an admin turned off by hand)."""
    if exit_row is not None:
        return exit_row.status(today)
    return "active" if user.is_active else "inactive"


# ── reading ────────────────────────────────────────────────────────────


@dataclass(slots=True)
class StaffRow:
    user: User
    exit: EmployeeExit | None
    status: str
    teams: list[str] = field(default_factory=list)
    payroll_code: str | None = None
    payroll_left_on: date | None = None


async def roster(session: AsyncSession) -> list[StaffRow]:
    """Everybody with a user record, in four queries however many there are."""
    today = office_today()
    users = list((await session.scalars(select(User).order_by(User.display_name))).all())
    exits = {
        row.user_id: row for row in (await session.scalars(select(EmployeeExit))).all()
    }
    teams: dict[uuid.UUID, set[str]] = {}
    for user_id, name in (
        await session.execute(
            select(TeamMembership.user_id, Team.name)
            .join(Team, Team.id == TeamMembership.team_id)
            .where(Team.archived_at.is_(None))
        )
    ).all():
        teams.setdefault(user_id, set()).add(name)
    payroll = {
        row.user_id: row
        for row in (
            await session.scalars(
                select(PayrollEmployee).where(PayrollEmployee.user_id.is_not(None))
            )
        ).all()
    }
    out: list[StaffRow] = []
    for user in users:
        exit_row = exits.get(user.id)
        pay = payroll.get(user.id)
        out.append(
            StaffRow(
                user=user,
                exit=exit_row,
                status=status_of(user, exit_row, today),
                teams=sorted(teams.get(user.id, set())),
                payroll_code=pay.code if pay else None,
                payroll_left_on=pay.left_on if pay else None,
            )
        )
    return out


async def one(session: AsyncSession, user_id: uuid.UUID) -> StaffRow:
    for row in await roster(session):
        if row.user.id == user_id:
            return row
    raise StaffError("No such person")


async def events(session: AsyncSession, user_id: uuid.UUID) -> list[EmploymentEvent]:
    return list(
        (
            await session.scalars(
                select(EmploymentEvent)
                .where(EmploymentEvent.user_id == user_id)
                .order_by(EmploymentEvent.created_at.desc())
            )
        ).all()
    )


# ── leaving ────────────────────────────────────────────────────────────


async def record_exit(
    session: AsyncSession,
    *,
    user: User,
    actor: User,
    kind: str,
    resigned_on: date | None,
    last_working_day: date,
    note: str | None,
) -> EmployeeExit:
    """Record, or correct, somebody's exit. Re-recording updates the dates."""
    if kind not in KIND_LABELS:
        raise StaffError(f"Unknown reason {kind!r}")
    if resigned_on and last_working_day < resigned_on:
        raise StaffError("The last working day cannot be before the day they resigned")

    row = await session.scalar(select(EmployeeExit).where(EmployeeExit.user_id == user.id))
    changed = row is not None
    if row is None:
        row = EmployeeExit(user_id=user.id, user=user, last_working_day=last_working_day)
        session.add(row)
    row.kind = kind
    row.resigned_on = resigned_on
    row.last_working_day = last_working_day
    row.note = (note or "").strip() or None
    row.recorded_by_id = actor.id

    payroll_note = await _payroll_leaving_date(session, row=row, user=user, actor=actor)

    last = last_working_day.strftime("%d %b %Y")
    session.add(
        EmploymentEvent(
            user_id=user.id,
            actor_id=actor.id,
            action="exit_changed" if changed else "exit_recorded",
            summary=(
                f"{KIND_LABELS[kind]}, last working day {last}"
                if not changed
                else f"Exit corrected: {KIND_LABELS[kind].lower()}, last working day {last}"
            )
            + (f". {payroll_note}" if payroll_note else ""),
            detail={
                "kind": kind,
                "resigned_on": resigned_on.isoformat() if resigned_on else None,
                "last_working_day": last_working_day.isoformat(),
                "note": row.note,
            },
        )
    )
    await session.flush()

    # A last day already behind: the account goes now, not at the next sweep.
    # A corrected date that moves it back into the future reopens it.
    today = office_today()
    if last_working_day < today:
        await _end_access(session, row, user, actor_id=actor.id)
    elif row.access_ended_at is not None:
        await _restore_access(session, row, user, actor_id=actor.id, why="last working day moved")
    return row


async def _we_switched_off(session: AsyncSession, row: EmployeeExit) -> bool:
    """Whether this module, not an admin's hand, turned the account off."""
    found = await session.scalar(
        select(EmploymentEvent.id)
        .where(
            EmploymentEvent.user_id == row.user_id,
            EmploymentEvent.action == "access_ended",
            EmploymentEvent.created_at >= row.created_at,
        )
        .limit(1)
    )
    return found is not None


async def withdraw_exit(session: AsyncSession, *, user: User, actor: User) -> None:
    """Resignation withdrawn, or somebody taken back after they left."""
    row = await session.scalar(select(EmployeeExit).where(EmployeeExit.user_id == user.id))
    if row is None:
        raise StaffError("No exit is recorded for this person")

    was = row.status(office_today())
    parts: list[str] = []
    if row.access_ended_at is not None:
        was_off = not user.is_active
        await _restore_access(session, row, user, actor_id=actor.id, why=None, log=False)
        if was_off and user.is_active:
            parts.append("sign-in switched back on")
    if row.payroll_employee_id and row.payroll_left_on_set:
        pay = await session.get(PayrollEmployee, row.payroll_employee_id)
        if pay is not None and pay.left_on == row.payroll_left_on_set:
            pay.left_on = None
            session.add(
                PayrollEvent(
                    actor_id=actor.id,
                    employee_id=pay.id,
                    action="left_on_cleared",
                    summary=f"Leaving date cleared: {user.display_name}'s exit was withdrawn in HR",
                    detail={"from_hr": True},
                )
            )
            parts.append("payroll leaving date cleared")

    session.add(
        EmploymentEvent(
            user_id=user.id,
            actor_id=actor.id,
            action="reinstated" if was == "left" else "exit_withdrawn",
            summary=("Taken back on" if was == "left" else "Resignation withdrawn")
            + (f": {', '.join(parts)}" if parts else ""),
            detail={"last_working_day": row.last_working_day.isoformat()},
        )
    )
    await session.delete(row)
    await session.flush()


async def end_access_for_leavers(session: AsyncSession) -> list[User]:
    """Switch off everyone whose last working day has passed. Idempotent."""
    today = office_today()
    rows = (
        await session.scalars(
            select(EmployeeExit).where(
                EmployeeExit.last_working_day < today,
                EmployeeExit.access_ended_at.is_(None),
            )
        )
    ).all()
    ended: list[User] = []
    for row in rows:
        await _end_access(session, row, row.user, actor_id=None)
        ended.append(row.user)
    await session.flush()
    return ended


async def refresh_rankings(session: AsyncSession, *, reason: str = "staff") -> bool:
    """Rebuild the stored rankings the next tender is chosen from.

    The same set the mirror rebuilds: company-wide plus every team with an
    enabled policy. False when it could not be done — the exit is recorded
    either way, and the next mirror sync rebuilds them.
    """
    from app.analytics import live  # the analytics package imports labels

    try:
        counted = await live.counts(session)
        await live.recompute(session, team=None, reason=reason, counted=counted)
        teams = (
            await session.scalars(
                select(Team)
                .join(AssignmentPolicy, AssignmentPolicy.team_id == Team.id)
                .where(AssignmentPolicy.enabled.is_(True), Team.archived_at.is_(None))
            )
        ).all()
        for team in teams:
            await live.recompute(session, team=team, reason=reason, counted=counted)
        return True
    except Exception:  # noqa: BLE001 - the exit itself must not be lost to this
        logger.exception("could not rebuild rankings after a staff change")
        # Callers commit the exit first; this only drops the half-built ranking.
        await session.rollback()
        return False


# ── internals ──────────────────────────────────────────────────────────


async def _payroll_leaving_date(
    session: AsyncSession, *, row: EmployeeExit, user: User, actor: User
) -> str | None:
    pay = await session.scalar(select(PayrollEmployee).where(PayrollEmployee.user_id == user.id))
    if pay is None:
        return None
    last = row.last_working_day
    ours = pay.left_on is not None and pay.left_on == row.payroll_left_on_set
    if pay.left_on is not None and not ours:
        if pay.left_on == last:
            return None
        return f"Payroll already has leaving date {pay.left_on.strftime('%d %b %Y')}; left as it is"
    if last < pay.joined_on:
        return "Payroll leaving date not set: it would be before their joining date"
    if pay.left_on == last:
        return None
    pay.left_on = last
    row.payroll_employee_id = pay.id
    row.payroll_left_on_set = last
    session.add(
        PayrollEvent(
            actor_id=actor.id,
            employee_id=pay.id,
            action="left_on_set",
            summary=f"Leaving date {last.strftime('%d %b %Y')} set from HR's exit record",
            detail={"from_hr": True, "left_on": last.isoformat()},
        )
    )
    return f"Payroll leaving date set to {last.strftime('%d %b %Y')}"


async def _end_access(
    session: AsyncSession, row: EmployeeExit, user: User, *, actor_id: uuid.UUID | None
) -> None:
    if row.access_ended_at is not None:
        return
    row.access_ended_at = datetime.now(UTC)
    if not user.is_active:
        return  # already off by somebody else's hand; nothing to log or undo
    user.is_active = False
    session.add(
        EmploymentEvent(
            user_id=user.id,
            actor_id=actor_id,
            action="access_ended",
            summary="Sign-in switched off: last working day passed",
            detail={"last_working_day": row.last_working_day.isoformat()},
        )
    )


async def _restore_access(
    session: AsyncSession,
    row: EmployeeExit,
    user: User,
    *,
    actor_id: uuid.UUID,
    why: str | None,
    log: bool = True,
) -> None:
    ours = await _we_switched_off(session, row)
    row.access_ended_at = None
    if not ours:
        return  # it was off before this module touched it; leave it so
    user.is_active = True
    if log:
        session.add(
            EmploymentEvent(
                user_id=user.id,
                actor_id=actor_id,
                action="access_restored",
                summary=f"Sign-in switched back on{f': {why}' if why else ''}",
                detail={},
            )
        )
