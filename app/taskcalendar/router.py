"""The task calendar API: its settings, a sync on demand, and the events it keeps.

Super admins only — it writes into people's calendars.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import CurrentUser
from app.core.db import get_session
from app.followups import service as followups
from app.models.user import User
from app.roles.catalogue import SUPER_ADMIN
from app.roles.deps import CurrentRoles
from app.taskcalendar import service

router = APIRouter(prefix="/task-calendar", tags=["task-calendar"])

Session = Annotated[AsyncSession, Depends(get_session)]


async def require_super_admin(user: CurrentUser, roles: CurrentRoles) -> User:
    if SUPER_ADMIN not in roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a super admin can manage the task calendar.",
        )
    return user


SuperAdmin = Annotated[User, Depends(require_super_admin)]


class CalendarSettingsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    enabled: bool
    reminder_minutes: int
    only_emails: list[str]
    only_title_contains: str
    last_run_at: datetime | None
    last_error: str | None
    team_name: str | None = None


class CalendarSettingsIn(BaseModel):
    enabled: bool | None = None
    reminder_minutes: int | None = None
    only_emails: list[str] | None = None
    only_title_contains: str | None = Field(default=None, max_length=200)


class SyncOut(BaseModel):
    ran: bool
    tasks_read: int
    created: int
    updated: int
    moved: int
    removed: int
    skipped_placeholder: int
    errors: list[str]


class CalendarEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    task_id: str
    task_title: str
    user_id: uuid.UUID
    user_name: str | None = None
    bcd_at: datetime
    reminder_minutes: int
    synced_at: datetime | None
    last_error: str | None


async def _settings_out(session: AsyncSession) -> CalendarSettingsOut:
    row = await service.get_settings(session)
    fs = await followups.get_settings(session)
    body = CalendarSettingsOut.model_validate(row)
    body.team_name = fs.team.name if fs.team else None
    return body


@router.get("/settings", response_model=CalendarSettingsOut, summary="The switch and the reminder")
async def read_settings(admin: SuperAdmin, session: Session) -> CalendarSettingsOut:
    out = await _settings_out(session)
    await session.commit()
    return out


@router.patch("/settings", response_model=CalendarSettingsOut, summary="Change them")
async def update_settings(body: CalendarSettingsIn, admin: SuperAdmin, session: Session) -> CalendarSettingsOut:
    try:
        await service.update_settings(session, actor_id=admin.id, changes=body.model_dump(exclude_unset=True))
    except service.CalendarSettingsError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    await session.commit()
    return await _settings_out(session)


@router.post("/sync", response_model=SyncOut, summary="Bring the calendars in line now")
async def sync_now(admin: SuperAdmin, session: Session, request: Request) -> SyncOut:
    report = await service.sync(
        session, sharepoint=request.app.state.sharepoint,
        calendar=request.app.state.task_calendar, force=True,
    )
    await session.commit()
    return SyncOut(**report.as_dict())


@router.get("/events", response_model=list[CalendarEventOut], summary="The events it keeps, soonest first")
async def list_events(admin: SuperAdmin, session: Session) -> list[CalendarEventOut]:
    out = []
    for event in await service.events(session):
        body = CalendarEventOut.model_validate(event)
        body.user_name = event.user.display_name if event.user else None
        out.append(body)
    return out
