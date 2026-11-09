"""HR's staff list: everybody, their status, and recording that someone is leaving.

HR only, like the rest of the module. Recording an exit is what takes somebody
out of assignment — see ``app.hr.staff`` for everything that follows from it.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.hr import staff
from app.hr.access import HRUser
from app.models.employment import ExitKind
from app.models.user import User

router = APIRouter(prefix="/hr/staff", tags=["hr"])

Session = Annotated[AsyncSession, Depends(get_session)]

Status = Literal["active", "notice", "left", "inactive"]


class ExitOut(BaseModel):
    kind: str
    kind_label: str
    resigned_on: date | None
    last_working_day: date
    note: str | None
    recorded_by_name: str | None
    recorded_at: datetime
    access_ended_at: datetime | None


class StaffOut(BaseModel):
    user_id: uuid.UUID
    name: str
    email: str
    status: Status
    is_active: bool
    joined_on: date | None
    last_login_at: datetime | None
    teams: list[str]
    payroll_code: str | None
    payroll_left_on: date | None
    exit: ExitOut | None


class EventOut(BaseModel):
    action: str
    summary: str
    actor_name: str | None
    at: datetime


class StaffDetailOut(StaffOut):
    events: list[EventOut]


class ExitIn(BaseModel):
    kind: ExitKind = ExitKind.RESIGNED
    resigned_on: date | None = None
    last_working_day: date
    note: str | None = Field(default=None, max_length=2000)


class ExitResultOut(BaseModel):
    person: StaffDetailOut
    #: False when the stored rankings could not be rebuilt just now. The
    #: exclusion still holds — the next mirror sync rebuilds them.
    ranking_refreshed: bool


def _out(row: staff.StaffRow) -> StaffOut:
    e = row.exit
    return StaffOut(
        user_id=row.user.id,
        name=row.user.display_name,
        email=row.user.email,
        status=row.status,  # type: ignore[arg-type]
        is_active=row.user.is_active,
        joined_on=row.user.joined_on,
        last_login_at=row.user.last_login_at,
        teams=row.teams,
        payroll_code=row.payroll_code,
        payroll_left_on=row.payroll_left_on,
        exit=(
            ExitOut(
                kind=e.kind,
                kind_label=staff.KIND_LABELS.get(e.kind, e.kind),
                resigned_on=e.resigned_on,
                last_working_day=e.last_working_day,
                note=e.note,
                recorded_by_name=e.recorded_by.display_name if e.recorded_by else None,
                recorded_at=e.updated_at,
                access_ended_at=e.access_ended_at,
            )
            if e is not None
            else None
        ),
    )


async def _detail(session: AsyncSession, user_id: uuid.UUID) -> StaffDetailOut:
    try:
        row = await staff.one(session, user_id)
    except staff.StaffError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    events = await staff.events(session, user_id)
    return StaffDetailOut(
        **_out(row).model_dump(),
        events=[
            EventOut(
                action=ev.action,
                summary=ev.summary,
                actor_name=ev.actor.display_name if ev.actor else None,
                at=ev.created_at,
            )
            for ev in events
        ],
    )


async def _user(session: AsyncSession, user_id: uuid.UUID) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such person")
    return user


@router.get("", response_model=list[StaffOut], summary="Everybody, with their status")
async def list_staff(
    _hr: HRUser,
    session: Session,
    status_: Annotated[Status | Literal["all"] | None, Query(alias="status")] = None,
) -> list[StaffOut]:
    rows = await staff.roster(session)
    if status_ and status_ != "all":
        rows = [r for r in rows if r.status == status_]
    return [_out(r) for r in rows]


@router.get("/{user_id}", response_model=StaffDetailOut, summary="One person, with history")
async def get_staff(user_id: uuid.UUID, _hr: HRUser, session: Session) -> StaffDetailOut:
    return await _detail(session, user_id)


@router.put(
    "/{user_id}/exit",
    response_model=ExitResultOut,
    summary="Record (or correct) that someone is leaving",
)
async def record_exit(
    user_id: uuid.UUID, payload: ExitIn, actor: HRUser, session: Session
) -> ExitResultOut:
    user = await _user(session, user_id)
    try:
        await staff.record_exit(
            session,
            user=user,
            actor=actor,
            kind=payload.kind,
            resigned_on=payload.resigned_on,
            last_working_day=payload.last_working_day,
            note=payload.note,
        )
    except staff.StaffError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    await session.commit()
    refreshed = await staff.refresh_rankings(session, reason="exit")
    await session.commit()
    return ExitResultOut(person=await _detail(session, user_id), ranking_refreshed=refreshed)


@router.delete(
    "/{user_id}/exit",
    response_model=ExitResultOut,
    summary="Withdraw a resignation, or take somebody back",
)
async def withdraw_exit(user_id: uuid.UUID, actor: HRUser, session: Session) -> ExitResultOut:
    user = await _user(session, user_id)
    try:
        await staff.withdraw_exit(session, user=user, actor=actor)
    except staff.StaffError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    await session.commit()
    refreshed = await staff.refresh_rankings(session, reason="exit")
    await session.commit()
    return ExitResultOut(person=await _detail(session, user_id), ranking_refreshed=refreshed)
