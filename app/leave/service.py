"""Leave: submitting, deciding, and the rules behind the decisions.

The rule HR asked for is "at most N people off on the same day". The subtlety is
that a request covers a *range*, so it must be checked day by day: a five-day
request is blocked if any single day inside it is already full, not if the range
as a whole is busy on average.

Auto-decision happens on submission. Within the limit, approved; over it,
rejected with the day and the names that caused it — a refusal that does not say
why is not a decision, it is an obstacle. HR overrides either way.
"""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.leave import (
    DecisionBy,
    LeaveRequest,
    LeaveSettings,
    LeaveStatus,
    LeaveType,
)
from app.models.team import TeamMembership
from app.models.user import User


class LeaveError(Exception):
    """A leave operation was refused. Safe to show a user."""


class LeaveNotFoundError(LeaveError):
    pass


class LeaveConflictError(LeaveError):
    pass


# ── settings ───────────────────────────────────────────────────────────


async def get_settings(session: AsyncSession) -> LeaveSettings:
    """The single settings row, created with defaults on first use."""
    settings = await session.get(LeaveSettings, 1)
    if settings is None:
        settings = LeaveSettings(id=1)
        session.add(settings)
        await session.flush()
    return settings


async def update_settings(
    session: AsyncSession, *, actor_id: uuid.UUID | None = None, **changes
) -> LeaveSettings:
    settings = await get_settings(session)

    if (value := changes.get("max_concurrent")) is not None:
        if value < 1:
            raise LeaveError("At least one person must be allowed off at a time")
        settings.max_concurrent = value
    if (value := changes.get("max_days_per_request")) is not None:
        if value < 1:
            raise LeaveError("A request must be allowed to cover at least one day")
        settings.max_days_per_request = value
    if (value := changes.get("limit_scope")) is not None:
        if value not in ("organisation", "team"):
            raise LeaveError("limit_scope must be 'organisation' or 'team'")
        settings.limit_scope = value
    for flag in ("auto_decide", "notify_hr_by_email"):
        if changes.get(flag) is not None:
            setattr(settings, flag, bool(changes[flag]))
    if (value := changes.get("hr_team_slug")) is not None:
        if not value.strip():
            raise LeaveError("hr_team_slug is required")
        settings.hr_team_slug = value.strip()

    settings.updated_by_id = actor_id
    await session.flush()
    return settings


# ── who is HR ──────────────────────────────────────────────────────────


async def hr_members(session: AsyncSession) -> list[User]:
    """Everyone in the HR team. They are the only ones who decide requests."""
    from app.teams import service as teams_service
    from app.teams.service import TeamError

    settings = await get_settings(session)
    try:
        team = await teams_service.get_team(session, settings.hr_team_slug)
    except TeamError:
        # Misconfigured or not created yet. Better an empty list than a crash on
        # every submission; the endpoints report it.
        return []
    return [user for user, _rows in await teams_service.list_members(session, team.id)]


async def is_hr(session: AsyncSession, user_id: uuid.UUID) -> bool:
    return any(u.id == user_id for u in await hr_members(session))


# ── the concurrency rule ───────────────────────────────────────────────


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=n) for n in range((end - start).days + 1)]


async def _peers(session: AsyncSession, user_id: uuid.UUID, scope: str) -> set[uuid.UUID] | None:
    """Whose leave counts against this request. None means everyone."""
    if scope != "team":
        return None

    team_ids = list(
        (
            await session.scalars(
                select(TeamMembership.team_id).where(TeamMembership.user_id == user_id).distinct()
            )
        ).all()
    )
    if not team_ids:
        # In no team, so nobody's leave is comparable. Only their own counts.
        return {user_id}
    peers = await session.scalars(
        select(TeamMembership.user_id).where(TeamMembership.team_id.in_(team_ids)).distinct()
    )
    return set(peers.all())


async def clashes(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    start: date,
    end: date,
    exclude_request_id: uuid.UUID | None = None,
) -> dict[date, list[User]]:
    """Who is already on approved leave, per day of the requested range."""
    settings = await get_settings(session)
    peers = await _peers(session, user_id, settings.limit_scope)

    query = (
        select(LeaveRequest)
        .where(
            LeaveRequest.status == LeaveStatus.APPROVED,
            LeaveRequest.user_id != user_id,
            # Ranges overlap unless one ends before the other starts.
            LeaveRequest.start_date <= end,
            LeaveRequest.end_date >= start,
        )
    )
    if peers is not None:
        query = query.where(LeaveRequest.user_id.in_(peers))
    if exclude_request_id is not None:
        query = query.where(LeaveRequest.id != exclude_request_id)

    overlapping = list((await session.scalars(query)).all())

    per_day: dict[date, list[User]] = {}
    for day in _days(start, end):
        who = [r.user for r in overlapping if r.start_date <= day <= r.end_date]
        if who:
            per_day[day] = who
    return per_day


def busiest(per_day: dict[date, list[User]]) -> tuple[date | None, list[User]]:
    """The single worst day in the range, which is what a decision turns on."""
    if not per_day:
        return None, []
    day = max(per_day, key=lambda d: (len(per_day[d]), d))
    return day, per_day[day]


# ── submitting ─────────────────────────────────────────────────────────


async def submit(
    session: AsyncSession,
    *,
    user: User,
    leave_type: LeaveType,
    start: date,
    end: date,
    reason: str | None = None,
) -> LeaveRequest:
    """Raise a request and decide it, if HR has automatic decisions switched on."""
    settings = await get_settings(session)

    if end < start:
        raise LeaveError("The end date cannot be before the start date")
    span = (end - start).days + 1
    if span > settings.max_days_per_request:
        raise LeaveError(
            f"A single request cannot cover more than {settings.max_days_per_request} days"
        )

    existing = await session.scalar(
        select(LeaveRequest).where(
            LeaveRequest.user_id == user.id,
            LeaveRequest.status.in_([LeaveStatus.PENDING, LeaveStatus.APPROVED]),
            LeaveRequest.start_date <= end,
            LeaveRequest.end_date >= start,
        )
    )
    if existing is not None:
        raise LeaveConflictError(
            f"You already have leave from {existing.start_date} to {existing.end_date} "
            f"overlapping these dates"
        )

    request = LeaveRequest(
        user_id=user.id,
        leave_type=leave_type,
        start_date=start,
        end_date=end,
        reason=reason,
        status=LeaveStatus.PENDING,
    )
    session.add(request)
    await session.flush()

    if settings.auto_decide:
        await auto_decide(session, request=request, settings=settings)

    return request


async def auto_decide(
    session: AsyncSession,
    *,
    request: LeaveRequest,
    settings: LeaveSettings | None = None,
) -> LeaveRequest:
    """Approve or reject by the concurrency rule, recording why."""
    settings = settings or await get_settings(session)
    per_day = await clashes(
        session,
        user_id=request.user_id,
        start=request.start_date,
        end=request.end_date,
        exclude_request_id=request.id,
    )
    day, people = busiest(per_day)
    already = len(people)

    request.conflicting_count = already
    request.decided_by = DecisionBy.SYSTEM
    request.decided_at = datetime.now(UTC)

    # `already` is how many are *already* off; this request would make it one more.
    if already + 1 <= settings.max_concurrent:
        request.status = LeaveStatus.APPROVED
        request.decision_note = (
            f"Approved automatically: at most {already} other "
            f"{'person is' if already == 1 else 'people are'} off on any day of this range, "
            f"within the limit of {settings.max_concurrent}."
        )
    else:
        names = ", ".join(sorted(u.display_name for u in people))
        request.status = LeaveStatus.REJECTED
        request.decision_note = (
            f"Rejected automatically: {already} people are already on leave on "
            f"{day.isoformat()} ({names}), and the limit is {settings.max_concurrent} "
            f"at a time. Ask HR if this is urgent — they can override it."
        )

    await session.flush()
    return request


# ── HR decisions ───────────────────────────────────────────────────────


async def get_request(session: AsyncSession, request_id: uuid.UUID) -> LeaveRequest:
    request = await session.get(LeaveRequest, request_id)
    if request is None:
        raise LeaveNotFoundError("No such leave request")
    return request


async def approve(
    session: AsyncSession,
    *,
    request: LeaveRequest,
    actor: User,
    note: str | None = None,
    emergency: bool = False,
) -> LeaveRequest:
    """HR says yes.

    ``emergency`` is what lets HR approve past the concurrency limit. Without it
    an over-limit approval is refused, so the rule cannot be bypassed by
    accident — only deliberately.
    """
    if request.status == LeaveStatus.CANCELLED:
        raise LeaveConflictError("That request was withdrawn by the requester")

    settings = await get_settings(session)
    per_day = await clashes(
        session,
        user_id=request.user_id,
        start=request.start_date,
        end=request.end_date,
        exclude_request_id=request.id,
    )
    day, people = busiest(per_day)

    if len(people) + 1 > settings.max_concurrent and not emergency:
        names = ", ".join(sorted(u.display_name for u in people))
        raise LeaveConflictError(
            f"{len(people)} people are already off on {day.isoformat()} ({names}), over the "
            f"limit of {settings.max_concurrent}. Approve as an emergency to override."
        )

    request.status = LeaveStatus.APPROVED
    request.decided_by = DecisionBy.HR
    request.decided_by_id = actor.id
    request.decided_at = datetime.now(UTC)
    request.emergency_override = bool(emergency)
    request.conflicting_count = len(people)
    request.decision_note = note or (
        f"Approved by {actor.display_name}"
        + (" as an emergency, overriding the concurrent limit." if emergency else ".")
    )
    await session.flush()
    return request


async def reject(
    session: AsyncSession, *, request: LeaveRequest, actor: User, note: str
) -> LeaveRequest:
    if not note or not note.strip():
        # A refusal with no reason is the thing people complain about most.
        raise LeaveError("A rejection needs a reason")
    if request.status == LeaveStatus.CANCELLED:
        raise LeaveConflictError("That request was withdrawn by the requester")

    request.status = LeaveStatus.REJECTED
    request.decided_by = DecisionBy.HR
    request.decided_by_id = actor.id
    request.decided_at = datetime.now(UTC)
    request.emergency_override = False
    request.decision_note = note.strip()
    await session.flush()
    return request


async def cancel(
    session: AsyncSession, *, request: LeaveRequest, actor: User
) -> LeaveRequest:
    """The requester withdraws their own request."""
    if request.user_id != actor.id:
        raise LeaveError("Only the person who requested it can withdraw it")
    if request.status == LeaveStatus.CANCELLED:
        return request
    if request.status == LeaveStatus.APPROVED and request.end_date < date.today():
        raise LeaveConflictError("That leave has already been taken")

    request.status = LeaveStatus.CANCELLED
    request.decided_by = DecisionBy.REQUESTER
    request.decided_by_id = actor.id
    request.decided_at = datetime.now(UTC)
    request.decision_note = "Withdrawn by the requester"
    await session.flush()
    return request


# ── listing ────────────────────────────────────────────────────────────


async def for_user(
    session: AsyncSession, user_id: uuid.UUID, *, limit: int = 100
) -> list[LeaveRequest]:
    return list(
        (
            await session.scalars(
                select(LeaveRequest)
                .where(LeaveRequest.user_id == user_id)
                .order_by(LeaveRequest.start_date.desc())
                .limit(limit)
            )
        ).all()
    )


async def all_requests(
    session: AsyncSession,
    *,
    status: LeaveStatus | None = None,
    upcoming_only: bool = False,
    limit: int = 200,
) -> list[LeaveRequest]:
    query = select(LeaveRequest).order_by(LeaveRequest.start_date.desc()).limit(limit)
    if status is not None:
        query = query.where(LeaveRequest.status == status)
    if upcoming_only:
        query = query.where(LeaveRequest.end_date >= date.today())
    return list((await session.scalars(query)).all())


async def calendar(
    session: AsyncSession, *, start: date, end: date
) -> dict[str, list[dict]]:
    """Who is off, day by day. Approved leave only — plans, not proposals."""
    rows = list(
        (
            await session.scalars(
                select(LeaveRequest).where(
                    LeaveRequest.status == LeaveStatus.APPROVED,
                    LeaveRequest.start_date <= end,
                    LeaveRequest.end_date >= start,
                )
            )
        ).all()
    )

    out: dict[str, list[dict]] = {}
    for day in _days(start, end):
        who = [
            {
                "user_id": str(r.user_id),
                "name": r.user.display_name,
                "leave_type": r.leave_type,
            }
            for r in rows
            if r.start_date <= day <= r.end_date
        ]
        if who:
            out[day.isoformat()] = who
    return out


async def summary(session: AsyncSession, user_id: uuid.UUID) -> dict:
    """A person's own totals, for their dashboard card."""
    rows = await for_user(session, user_id, limit=500)
    counts = Counter(r.status for r in rows)
    today = date.today()
    return {
        "total": len(rows),
        "pending": counts.get(LeaveStatus.PENDING, 0),
        "approved": counts.get(LeaveStatus.APPROVED, 0),
        "rejected": counts.get(LeaveStatus.REJECTED, 0),
        "cancelled": counts.get(LeaveStatus.CANCELLED, 0),
        "days_approved": sum(r.days for r in rows if r.status == LeaveStatus.APPROVED),
        "upcoming": [
            {
                "id": str(r.id),
                "start_date": r.start_date.isoformat(),
                "end_date": r.end_date.isoformat(),
                "leave_type": r.leave_type,
                "status": r.status,
            }
            for r in sorted(rows, key=lambda r: r.start_date)
            if r.end_date >= today and r.status in (LeaveStatus.APPROVED, LeaveStatus.PENDING)
        ][:5],
    }
