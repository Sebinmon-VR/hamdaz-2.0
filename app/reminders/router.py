"""The status reminder API: the form a person answers, and the settings.

Who sees what:

* **the person reminded** sees and answers their own — the only one who may;
* **their team's managers and leads**, and administrators, read them;
* **a super admin** sets when it runs and whether answers reach the list,
  runs it on demand, and tries it on one of their own tasks.

Answering is always as the caller, on a reminder addressed to them.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import CurrentUser
from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.followups import service as followups
from app.models.status_reminder import StatusReminder
from app.models.user import User
from app.reminders import service
from app.reminders.schemas import (
    AnswerIn,
    LiveTaskOut,
    ReminderFormOut,
    ReminderOut,
    ReminderSettingsIn,
    ReminderSettingsOut,
    RunOut,
    TaskFields,
    TryIn,
)
from app.roles.catalogue import SUPER_ADMIN
from app.roles.deps import CurrentRoles

router = APIRouter(prefix="/reminders", tags=["reminders"])

Session = Annotated[AsyncSession, Depends(get_session)]
Config = Annotated[Settings, Depends(get_settings)]


async def require_super_admin(user: CurrentUser, roles: CurrentRoles) -> User:
    if SUPER_ADMIN not in roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a super admin can change the status reminder.",
        )
    return user


SuperAdmin = Annotated[User, Depends(require_super_admin)]


def _translate(exc: service.ReminderError) -> HTTPException:
    if isinstance(exc, service.ReminderNotFound):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, service.ReminderForbidden):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


def _out(row: StatusReminder, viewer: User) -> ReminderOut:
    body = ReminderOut.model_validate(row)
    body.assignee_name = row.assignee.display_name if row.assignee else None
    body.may_answer = row.assignee_id == viewer.id and row.is_open
    return body


def _mailer(request: Request):
    return request.app.state.reminder_worker.mailer


# ── settings — before /{id}, which would otherwise read "settings" as an id ──


async def _settings_out(session: AsyncSession) -> ReminderSettingsOut:
    row = await service.get_settings(session)
    fs = await followups.get_settings(session)
    body = ReminderSettingsOut.model_validate(row)
    body.team_name = fs.team.name if fs.team else None
    body.timezone = fs.digest_timezone
    body.test_mail_to = fs.test_mail_to
    return body


@router.get("/settings", response_model=ReminderSettingsOut, summary="When it runs, and what it writes")
async def read_settings(admin: SuperAdmin, session: Session) -> ReminderSettingsOut:
    out = await _settings_out(session)
    await session.commit()
    return out


@router.patch("/settings", response_model=ReminderSettingsOut, summary="Change it")
async def update_settings(
    body: ReminderSettingsIn, admin: SuperAdmin, session: Session
) -> ReminderSettingsOut:
    try:
        await service.update_settings(
            session, actor_id=admin.id, changes=body.model_dump(exclude_unset=True)
        )
    except service.ReminderError as exc:
        raise _translate(exc) from exc
    await session.commit()
    return await _settings_out(session)


@router.post("/run", response_model=RunOut, summary="Send today's reminders now")
async def run_now(
    admin: SuperAdmin, session: Session, request: Request, config: Config
) -> RunOut:
    """For trying it out: remind about whatever qualifies right now, switched
    on or not. Every filter still applies, so it reaches nobody the settings
    would not. It does not count as the day's run."""
    report = await service.run(
        session,
        settings=config,
        sharepoint=request.app.state.sharepoint,
        mailer=_mailer(request),
        force=True,
    )
    await session.commit()
    return RunOut(**report.as_dict())


@router.post("/try", response_model=ReminderOut, summary="Remind me about one of my tasks, now")
async def try_on_my_task(
    body: TryIn, admin: SuperAdmin, session: Session, request: Request, config: Config
) -> ReminderOut:
    """One of the caller's own tasks, reminded about at once — from and to
    the caller only."""
    try:
        row = await service.ask_now(
            session,
            user=admin,
            task_id=body.task_id,
            settings=config,
            sharepoint=request.app.state.sharepoint,
            mailer=_mailer(request),
        )
    except service.ReminderError as exc:
        raise _translate(exc) from exc
    await session.commit()
    await session.refresh(row)
    return _out(row, admin)


@router.get("/recent", response_model=list[ReminderOut], summary="The latest reminders and answers")
async def recent(admin: SuperAdmin, session: Session) -> list[ReminderOut]:
    return [_out(r, admin) for r in await service.recent(session)]


@router.get("/mine", response_model=list[ReminderOut], summary="The ones sent to me")
async def mine(user: CurrentUser, session: Session) -> list[ReminderOut]:
    return [_out(r, user) for r in await service.mine(session, user.id)]


# ── one reminder ───────────────────────────────────────────────────────


async def _visible(
    reminder_id: uuid.UUID, user: User, roles: set[str], session: AsyncSession
) -> StatusReminder:
    try:
        row = await service.get(session, reminder_id)
    except service.ReminderError as exc:
        raise _translate(exc) from exc
    if not await service.may_see(session, row, user=user, roles=roles):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="There is no such reminder.")
    return row


@router.get("/{reminder_id}", response_model=ReminderFormOut, summary="One reminder, and the task now")
async def read_reminder(
    reminder_id: uuid.UUID, user: CurrentUser, roles: CurrentRoles, session: Session,
    request: Request,
) -> ReminderFormOut:
    row = await _visible(reminder_id, user, roles, session)
    settings_row = await service.get_settings(session)
    out = ReminderFormOut(reminder=_out(row, user), task=None)
    try:
        current = await service.live(request.app.state.sharepoint, row.task_id)
    except service.ReminderError as exc:
        out.task_error = str(exc)
    else:
        v = current.values
        out.task = LiveTaskOut(
            status=v[service.STATUS],
            submission_status=v[service.SUBMISSION],
            remarks=v[service.REMARKS],
            working_notes=v[service.WORKING_NOTES],
            status_choices=current.choices[service.STATUS],
            submission_choices=current.choices[service.SUBMISSION],
            writes_to_sharepoint=settings_row.write_sharepoint,
        )
    await session.commit()
    return out


def _by_column(fields: TaskFields) -> dict[str, str]:
    """The form's fields under their SharePoint names, leaving out the unset."""
    named = {
        service.STATUS: fields.status,
        service.SUBMISSION: fields.submission_status,
        service.REMARKS: fields.remarks,
        service.WORKING_NOTES: fields.working_notes,
    }
    return {k: v for k, v in named.items() if v is not None}


@router.post("/{reminder_id}/answer", response_model=ReminderOut, summary="Give the task's status")
async def answer(
    reminder_id: uuid.UUID,
    body: AnswerIn,
    user: CurrentUser,
    roles: CurrentRoles,
    session: Session,
    request: Request,
    config: Config,
) -> ReminderOut:
    row = await _visible(reminder_id, user, roles, session)
    try:
        await service.answer(
            session, row,
            user=user,
            wanted=_by_column(body.values),
            seen=_by_column(body.seen),
            sharepoint=request.app.state.sharepoint,
            settings=config,
            mailer=_mailer(request),
        )
    except service.ReminderError as exc:
        raise _translate(exc) from exc
    await session.commit()
    await session.refresh(row)
    return _out(row, user)
