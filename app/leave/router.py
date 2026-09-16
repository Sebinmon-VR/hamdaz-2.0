"""Leave: everyone requests, HR decides.

Access is deliberately asymmetric and does not use the module-visibility system:
leave is for *everyone*, so gating it behind a team grant would be wrong. What is
gated is deciding — that requires membership of the HR team, which HR itself
names in the settings.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.access.deps import module_guard
from app.auth.deps import CurrentUser
from app.core.db import get_session
from app.leave import service
from app.leave.mailer import LeaveMailer, MailError
from app.leave.schemas import (
    CalendarOut,
    DecideIn,
    LeaveRequestIn,
    LeaveRequestOut,
    LeaveSettingsIn,
    LeaveSettingsOut,
    LeaveSummaryOut,
    RejectIn,
)
from app.leave.service import LeaveConflictError, LeaveError, LeaveNotFoundError
from app.models.leave import LeaveRequest, LeaveStatus
from app.models.user import User

router = APIRouter(
    prefix="/leave",
    tags=["leave"],
    # On the router rather than on each route, so a route added later
    # cannot quietly miss it. See app/access/deps.py.
    dependencies=[Depends(module_guard("leave", "Leave"))],
)

Session = Annotated[AsyncSession, Depends(get_session)]


def get_mailer(request: Request) -> LeaveMailer:
    return request.app.state.leave_mailer


def _translate(exc: LeaveError) -> HTTPException:
    if isinstance(exc, LeaveNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, LeaveConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


async def require_hr(user: CurrentUser, session: Session) -> User:
    """Only the HR team decides leave."""
    if not await service.is_hr(session, user.id):
        settings = await service.get_settings(session)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Only the {settings.hr_team_slug!r} team can do that. "
                f"Ask a super admin to add you."
            ),
        )
    return user


HRUser = Annotated[User, Depends(require_hr)]


def _out(request: LeaveRequest) -> LeaveRequestOut:
    return LeaveRequestOut(
        id=request.id,
        user_id=request.user_id,
        user_name=request.user.display_name,
        user_email=request.user.email,
        leave_type=request.leave_type,
        start_date=request.start_date,
        end_date=request.end_date,
        days=request.days,
        reason=request.reason,
        status=request.status,
        decided_by=request.decided_by,
        decided_by_id=request.decided_by_id,
        decided_at=request.decided_at,
        decision_note=request.decision_note,
        emergency_override=request.emergency_override,
        conflicting_count=request.conflicting_count,
        notified_at=request.notified_at,
        notify_error=request.notify_error,
        created_at=request.created_at,
    )


async def _notify(session: AsyncSession, mailer: LeaveMailer, request: LeaveRequest) -> None:
    """Mail HR, if HR has asked for it. Never fails the request."""
    settings = await service.get_settings(session)
    if not settings.notify_hr_by_email:
        return

    try:
        recipients = [u.email for u in await service.hr_members(session) if u.email]
        await mailer.send_request(request, recipients)
        request.notified_at = datetime.now(UTC)
        request.notify_error = None
    except (MailError, Exception) as exc:  # noqa: BLE001 - the leave still stands
        request.notify_error = f"{type(exc).__name__}: {exc}"[:500]
    await session.flush()


# ── requesting ─────────────────────────────────────────────────────────


@router.post(
    "/requests",
    response_model=LeaveRequestOut,
    status_code=status.HTTP_201_CREATED,
    summary="Request leave",
)
async def create_request(
    payload: LeaveRequestIn,
    user: CurrentUser,
    session: Session,
    mailer: Annotated[LeaveMailer, Depends(get_mailer)],
) -> LeaveRequestOut:
    """Open to every signed-in user — leave is not a team privilege."""
    try:
        request = await service.submit(
            session,
            user=user,
            leave_type=payload.leave_type,
            start=payload.start_date,
            end=payload.end_date,
            reason=payload.reason,
        )
    except LeaveError as exc:
        raise _translate(exc) from exc

    await _notify(session, mailer, request)
    return _out(request)


@router.get("/requests/me", response_model=list[LeaveRequestOut], summary="My requests")
async def my_requests(user: CurrentUser, session: Session) -> list[LeaveRequestOut]:
    return [_out(r) for r in await service.for_user(session, user.id)]


@router.get("/summary/me", response_model=LeaveSummaryOut, summary="My leave at a glance")
async def my_summary(user: CurrentUser, session: Session) -> LeaveSummaryOut:
    return LeaveSummaryOut(**await service.summary(session, user.id))


@router.post(
    "/requests/{request_id}/cancel",
    response_model=LeaveRequestOut,
    summary="Withdraw my own request",
)
async def cancel_request(
    request_id: uuid.UUID, user: CurrentUser, session: Session
) -> LeaveRequestOut:
    try:
        request = await service.get_request(session, request_id)
        await service.cancel(session, request=request, actor=user)
    except LeaveError as exc:
        raise _translate(exc) from exc
    return _out(request)


# ── HR ─────────────────────────────────────────────────────────────────


@router.get("/requests", response_model=list[LeaveRequestOut], summary="All requests (HR)")
async def list_requests(
    _: HRUser,
    session: Session,
    request_status: Annotated[LeaveStatus | None, Query(alias="status")] = None,
    upcoming_only: Annotated[bool, Query()] = False,
) -> list[LeaveRequestOut]:
    rows = await service.all_requests(
        session, status=request_status, upcoming_only=upcoming_only
    )
    return [_out(r) for r in rows]


@router.post(
    "/requests/{request_id}/approve",
    response_model=LeaveRequestOut,
    summary="Approve a request (HR)",
)
async def approve_request(
    request_id: uuid.UUID, payload: DecideIn, actor: HRUser, session: Session
) -> LeaveRequestOut:
    """Approving past the concurrent limit requires ``emergency``.

    Without that flag an over-limit approval is refused, so the rule cannot be
    stepped over by accident — only on purpose, and it is recorded as such.
    """
    try:
        request = await service.get_request(session, request_id)
        await service.approve(
            session,
            request=request,
            actor=actor,
            note=payload.note,
            emergency=payload.emergency,
        )
    except LeaveError as exc:
        raise _translate(exc) from exc
    return _out(request)


@router.post(
    "/requests/{request_id}/reject",
    response_model=LeaveRequestOut,
    summary="Reject a request (HR)",
)
async def reject_request(
    request_id: uuid.UUID, payload: RejectIn, actor: HRUser, session: Session
) -> LeaveRequestOut:
    try:
        request = await service.get_request(session, request_id)
        await service.reject(session, request=request, actor=actor, note=payload.note)
    except LeaveError as exc:
        raise _translate(exc) from exc
    return _out(request)


# ── the shared view ────────────────────────────────────────────────────


@router.get("/calendar", response_model=CalendarOut, summary="Who is off, day by day")
async def leave_calendar(
    _: CurrentUser,
    session: Session,
    start: Annotated[date | None, Query()] = None,
    days: Annotated[int, Query(ge=1, le=180)] = 30,
) -> CalendarOut:
    # Open to everyone: knowing who is away is what stops people booking over
    # each other in the first place.
    begin = start or date.today()
    finish = begin + timedelta(days=days - 1)
    return CalendarOut(
        start=begin, end=finish, days=await service.calendar(session, start=begin, end=finish)
    )


# ── settings ───────────────────────────────────────────────────────────


@router.get("/settings", response_model=LeaveSettingsOut, summary="The leave rules")
async def read_settings(_: CurrentUser, session: Session) -> LeaveSettingsOut:
    # Readable by everyone: people should be able to see the rule that decided
    # their request.
    settings = await service.get_settings(session)
    return LeaveSettingsOut.model_validate(settings)


@router.put("/settings", response_model=LeaveSettingsOut, summary="Change the rules (HR)")
async def write_settings(
    payload: LeaveSettingsIn, actor: HRUser, session: Session
) -> LeaveSettingsOut:
    try:
        settings = await service.update_settings(
            session, actor_id=actor.id, **payload.model_dump(exclude_none=True)
        )
    except LeaveError as exc:
        raise _translate(exc) from exc
    return LeaveSettingsOut.model_validate(settings)
