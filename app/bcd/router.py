"""The BCD check API: the confirm page, the open list, and the settings.

* **the assignee, the team's lead, managers and approvers, and admins** open a
  check and may confirm the date as it stands;
* **a super admin** sets the hours and the switch, runs it on demand, and
  tries it on one of their own tasks.

Nothing here writes to SharePoint: the BCD is corrected in the list itself.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import CurrentUser
from app.bcd import service
from app.bcd.schemas import (
    BcdCheckOut,
    BcdFormOut,
    BcdRunOut,
    BcdSettingsIn,
    BcdSettingsOut,
    BcdTryIn,
)
from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.followups import service as followups
from app.models.bcd_check import BcdCheck
from app.models.user import User
from app.roles.catalogue import SUPER_ADMIN
from app.roles.deps import CurrentRoles

router = APIRouter(prefix="/bcd-checks", tags=["bcd-checks"])

Session = Annotated[AsyncSession, Depends(get_session)]
Config = Annotated[Settings, Depends(get_settings)]


async def require_super_admin(user: CurrentUser, roles: CurrentRoles) -> User:
    if SUPER_ADMIN not in roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a super admin can change the BCD check.",
        )
    return user


SuperAdmin = Annotated[User, Depends(require_super_admin)]


def _translate(exc: service.BcdError) -> HTTPException:
    if isinstance(exc, service.BcdNotFound):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, service.BcdForbidden):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


def _out(row: BcdCheck, *, may_confirm: bool = False) -> BcdCheckOut:
    body = BcdCheckOut.model_validate(row)
    body.assignee_name = row.assignee.display_name if row.assignee else None
    body.resolved_by_name = row.resolved_by.display_name if row.resolved_by else None
    body.edit_url = service.edit_link(row.task_url)
    body.may_confirm = may_confirm and row.is_open
    return body


def _mailer(request: Request):
    return request.app.state.bcd_worker.mailer


# ── settings — before /{id} ──────────────────────────────────────────


async def _settings_out(session: AsyncSession) -> BcdSettingsOut:
    row = await service.get_settings(session)
    fs = await followups.get_settings(session)
    body = BcdSettingsOut.model_validate(row)
    body.team_name = fs.team.name if fs.team else None
    body.team_leads = await service.team_leads(session, fs.team_id)
    body.escalate_to = await service.escalation_list(session, fs.team_id)
    return body


@router.get("/settings", response_model=BcdSettingsOut, summary="Hours, switch and who hears")
async def read_settings(admin: SuperAdmin, session: Session) -> BcdSettingsOut:
    out = await _settings_out(session)
    await session.commit()
    return out


@router.patch("/settings", response_model=BcdSettingsOut, summary="Change them")
async def update_settings(body: BcdSettingsIn, admin: SuperAdmin, session: Session) -> BcdSettingsOut:
    try:
        await service.update_settings(session, actor_id=admin.id, changes=body.model_dump(exclude_unset=True))
    except service.BcdError as exc:
        raise _translate(exc) from exc
    await session.commit()
    return await _settings_out(session)


@router.post("/run", response_model=BcdRunOut, summary="Check now, switched on or not")
async def run_now(admin: SuperAdmin, session: Session, request: Request, config: Config) -> BcdRunOut:
    report = await service.run(
        session, settings=config, sharepoint=request.app.state.sharepoint,
        mailer=_mailer(request), force=True,
    )
    await session.commit()
    return BcdRunOut(**report.as_dict())


@router.post("/try", response_model=BcdCheckOut, summary="Ask me about one of my tasks, now")
async def try_on_my_task(
    body: BcdTryIn, admin: SuperAdmin, session: Session, request: Request, config: Config
) -> BcdCheckOut:
    try:
        row = await service.try_on(
            session, user=admin, task_id=body.task_id, settings=config,
            sharepoint=request.app.state.sharepoint, mailer=_mailer(request),
        )
    except service.BcdError as exc:
        raise _translate(exc) from exc
    await session.commit()
    await session.refresh(row)
    return _out(row, may_confirm=True)


@router.get("", response_model=list[BcdCheckOut], summary="Open checks first, then the latest")
async def list_checks(user: CurrentUser, roles: CurrentRoles, session: Session) -> list[BcdCheckOut]:
    rows = await service.open_checks(session)
    out = []
    for row in rows:
        if await service.may_act(session, row, user=user, roles=roles):
            out.append(_out(row, may_confirm=True))
    return out


# ── one check ─────────────────────────────────────────────────────────


async def _visible(check_id: uuid.UUID, user: User, roles: set[str], session: AsyncSession) -> BcdCheck:
    try:
        row = await service.get(session, check_id)
    except service.BcdError as exc:
        raise _translate(exc) from exc
    if not await service.may_act(session, row, user=user, roles=roles):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="There is no such BCD check.")
    return row


@router.get("/{check_id}", response_model=BcdFormOut, summary="One check, and the BCD on the list now")
async def read_check(
    check_id: uuid.UUID, user: CurrentUser, roles: CurrentRoles, session: Session, request: Request,
) -> BcdFormOut:
    row = await _visible(check_id, user, roles, session)
    out = BcdFormOut(check=_out(row, may_confirm=True))
    try:
        task = await request.app.state.sharepoint.task(row.task_id)
    except Exception:  # noqa: BLE001
        out.task_error = "The task could not be read from the Proposals list just now."
    else:
        # As the follow-ups read it — the clock typed, in the UAE.
        from app.followups.mailer import _when

        out.current_bcd = _when(followups.due_of(task)) if task.bid_closing_date else None
        out.still_placeholder = service.still_unset(row, task)
        # Corrected on the list since: closed here and now, not in five minutes.
        if row.is_open and service.resolve_against(row, task, datetime.now(UTC)):
            await session.flush()
            out.check = _out(row, may_confirm=True)
    await session.commit()
    return out


@router.post("/{check_id}/confirm", response_model=BcdCheckOut, summary="The BCD as it stands is right")
async def confirm(
    check_id: uuid.UUID, user: CurrentUser, roles: CurrentRoles, session: Session
) -> BcdCheckOut:
    row = await _visible(check_id, user, roles, session)
    try:
        await service.confirm(session, row, user=user)
    except service.BcdError as exc:
        raise _translate(exc) from exc
    await session.commit()
    await session.refresh(row)
    return _out(row, may_confirm=True)
