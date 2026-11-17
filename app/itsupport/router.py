"""The IT Support settings API, for /admin/it-support. Super admins only:
it decides whose Teams chats an AI answers and what it may do in the ERP."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import CurrentUser
from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.itsupport import settings as service
from app.models.user import User
from app.roles.catalogue import SUPER_ADMIN
from app.roles.deps import CurrentRoles

router = APIRouter(prefix="/it-support", tags=["it-support"])

Session = Annotated[AsyncSession, Depends(get_session)]
Config = Annotated[Settings, Depends(get_settings)]


async def require_super_admin(user: CurrentUser, roles: CurrentRoles) -> User:
    if SUPER_ADMIN not in roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a super admin can manage IT Support.",
        )
    return user


SuperAdmin = Annotated[User, Depends(require_super_admin)]


class PersonIn(BaseModel):
    email: str = Field(max_length=320)
    name: str = Field(default="", max_length=60)


class PersonOut(PersonIn):
    #: The agent works the ERP as them; without an account it can only chat.
    has_account: bool


class ModuleOut(BaseModel):
    key: str
    name: str
    default: str
    max: str
    mode: str


class ITSupportSettingsOut(BaseModel):
    enabled: bool
    people: list[PersonOut]
    show_timing: bool
    due_enabled: bool
    due_from: str
    due_until: str
    due_days: list[int]
    due_test_to: str
    brain_model: str
    brain_effort: str
    modules: list[ModuleOut]
    models: list[str]
    efforts: list[str]
    # the worker's heartbeat
    running: bool
    running_on: str | None
    last_seen_at: datetime | None
    signed_in_as: str | None
    last_error: str | None
    #: Whether this server itself may run it (IT_SUPPORT_CHAT_ENABLED).
    runs_here: bool


class ITSupportSettingsIn(BaseModel):
    enabled: bool | None = None
    people: list[PersonIn] | None = None
    show_timing: bool | None = None
    due_enabled: bool | None = None
    due_from: str | None = None
    due_until: str | None = None
    due_days: list[int] | None = None
    due_test_to: str | None = None
    brain_model: str | None = None
    brain_effort: str | None = None
    #: Each module's mode; anything left out goes back to its default.
    module_modes: dict[str, str] | None = None


async def _out(session: AsyncSession, config: Settings) -> ITSupportSettingsOut:
    row = await service.get_row(session, config)
    people = row.people or []
    have = await service.accounts(session, [p["email"] for p in people])
    saved: dict[str, Any] = row.module_modes or {}
    modules = [ModuleOut(**m, mode=saved.get(m["key"], m["default"]))
               for m in service.module_rows()]
    seen = row.last_seen_at
    return ITSupportSettingsOut(
        enabled=row.enabled,
        people=[PersonOut(email=p["email"], name=p.get("name", ""),
                          has_account=p["email"] in have) for p in people],
        show_timing=row.show_timing, due_enabled=row.due_enabled, due_from=row.due_from,
        due_until=row.due_until, due_days=list(row.due_days or []),
        due_test_to=row.due_test_to, brain_model=row.brain_model,
        brain_effort=row.brain_effort, modules=modules, models=list(service.MODELS),
        efforts=list(service.EFFORTS),
        running=seen is not None
        and (datetime.now(UTC) - seen).total_seconds() < service.ALIVE_SECONDS,
        running_on=row.running_on, last_seen_at=seen, signed_in_as=row.signed_in_as,
        last_error=row.last_error, runs_here=config.it_support_chat_enabled,
    )


@router.get("/settings", response_model=ITSupportSettingsOut,
            summary="Who it answers, the check-ins, the model, module access, and whether it runs")
async def read_settings(admin: SuperAdmin, session: Session,
                        config: Config) -> ITSupportSettingsOut:
    out = await _out(session, config)
    await session.commit()
    return out


@router.patch("/settings", response_model=ITSupportSettingsOut, summary="Change them")
async def update_settings(body: ITSupportSettingsIn, admin: SuperAdmin, session: Session,
                          config: Config) -> ITSupportSettingsOut:
    changes = body.model_dump(exclude_unset=True)
    try:
        await service.update(session, config, actor_id=admin.id, changes=changes)
    except service.ITSupportSettingsError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    await session.commit()
    return await _out(session, config)
