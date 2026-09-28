"""The follow-up API: the form a person answers, the team's list, the settings.

Who sees what:

* **the person asked** sees and answers their own — the only one who may;
* **their team's managers and leads**, and administrators, read them;
* **a super admin** sets what is watched, and may run a sweep on demand.

No route names a person to act on behalf of: answering is always as the
caller, and only on a follow-up addressed to them.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import CurrentUser
from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.followups import service
from app.followups.schemas import (
    DueTodayOut,
    FalsePositiveIn,
    FollowupOut,
    FollowupSettingsIn,
    FollowupSettingsOut,
    ReasonIn,
    SweepOut,
    TryIn,
)
from app.models.followup import TaskFollowup
from app.models.team import Team
from app.models.user import User
from app.roles.catalogue import SUPER_ADMIN
from app.roles.deps import CurrentRoles

router = APIRouter(prefix="/followups", tags=["followups"])

Session = Annotated[AsyncSession, Depends(get_session)]
Config = Annotated[Settings, Depends(get_settings)]


async def require_super_admin(user: CurrentUser, roles: CurrentRoles) -> User:
    if SUPER_ADMIN not in roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a super admin can change what the overdue follow-up watches.",
        )
    return user


SuperAdmin = Annotated[User, Depends(require_super_admin)]


def _translate(exc: service.FollowupError) -> HTTPException:
    if isinstance(exc, service.FollowupNotFound):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, service.FollowupForbidden):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


def _out(row: TaskFollowup, viewer: User) -> FollowupOut:
    body = FollowupOut.model_validate(row)
    body.team_name = row.team.name if row.team else None
    body.assignee_name = row.assignee.display_name if row.assignee else None
    body.may_answer = row.assignee_id == viewer.id and row.is_open
    return body


def _settings_out(row) -> FollowupSettingsOut:
    body = FollowupSettingsOut.model_validate(row)
    body.team_name = row.team.name if row.team else None
    body.ask_from_email = row.ask_from.email if row.ask_from else None
    return body


def _mailer(request: Request):
    return request.app.state.followup_worker.mailer


# ── settings — before /{id}, which would otherwise read "settings" as an id ──


@router.get("/settings", response_model=FollowupSettingsOut, summary="What is watched")
async def read_settings(admin: SuperAdmin, session: Session) -> FollowupSettingsOut:
    row = await service.get_settings(session)
    await session.commit()
    await session.refresh(row)
    return _settings_out(row)


@router.patch("/settings", response_model=FollowupSettingsOut, summary="Change it")
async def update_settings(
    body: FollowupSettingsIn, admin: SuperAdmin, session: Session
) -> FollowupSettingsOut:
    try:
        row = await service.update_settings(
            session, actor_id=admin.id, changes=body.model_dump(exclude_unset=True)
        )
    except service.FollowupError as exc:
        raise _translate(exc) from exc
    await session.commit()
    await session.refresh(row)
    return _settings_out(row)


@router.post("/run", response_model=SweepOut, summary="Sweep now, even if switched off")
async def run_now(
    admin: SuperAdmin, session: Session, request: Request, config: Config
) -> SweepOut:
    """For trying it out: ask about whatever qualifies right now.

    It still honours every filter — the team, the named people, the title
    word, the watch window and the grace — so running it cannot reach anybody
    the settings would not.
    """
    report = await service.sweep(
        session,
        settings=config,
        sharepoint=request.app.state.sharepoint,
        mailer=_mailer(request),
        force=True,
    )
    await session.commit()
    return SweepOut(**report.as_dict())


@router.post("/try", response_model=FollowupOut, summary="Ask me about one of my tasks, now")
async def try_on_my_task(
    body: TryIn, admin: SuperAdmin, session: Session, request: Request, config: Config
) -> FollowupOut:
    """The test path: one of the caller's own tasks, asked about immediately.

    Super admin only, and only on a task assigned to the caller — so trying it
    out can only ever mail the person trying it.
    """
    try:
        row = await service.ask_now(
            session,
            user=admin,
            task_id=body.task_id,
            settings=config,
            sharepoint=request.app.state.sharepoint,
            mailer=_mailer(request),
        )
    except service.FollowupError as exc:
        raise _translate(exc) from exc
    await session.commit()
    await session.refresh(row)
    return _out(row, admin)


# ── listings ───────────────────────────────────────────────────────────


@router.get("/mine", response_model=list[FollowupOut], summary="The ones sent to me")
async def my_followups(user: CurrentUser, session: Session) -> list[FollowupOut]:
    return [_out(r, user) for r in await service.mine(session, user.id)]


@router.get("/team", response_model=list[FollowupOut], summary="A team's, for its managers")
async def team_followups(
    user: CurrentUser,
    roles: CurrentRoles,
    session: Session,
    team: Annotated[str, Query(description="The team's slug (or id)")],
) -> list[FollowupOut]:
    from app.proposals.oversight import oversight
    from app.teams import service as teams_service
    from app.teams.service import TeamError

    try:
        resolved = await teams_service.get_team(session, team)
    except TeamError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    allowed = await oversight(session, user=user, roles=roles, team_id=resolved.id)
    if not allowed.may_see:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only this team's manager or lead, or an administrator, reads its follow-ups.",
        )
    return [_out(r, user) for r in await service.for_team(session, resolved.id)]


@router.get("/due-today", response_model=DueTodayOut, summary="Tasks due today, with their due times")
async def due_today(
    user: CurrentUser,
    roles: CurrentRoles,
    session: Session,
    request: Request,
    team: Annotated[str | None, Query(description="The team's slug; the watched team if left out")] = None,
    refresh: bool = False,
) -> DueTodayOut:
    """Today's due tasks, for the countdown.

    The team's managers, leads and administrators see every member's; anybody
    else sees their own only — the same rule as the team task board, so this
    cannot show anybody a colleague's work they could not already see.
    """
    from datetime import UTC, datetime

    from sqlalchemy import select

    from app.proposals.oversight import oversight, resolve_members
    from app.teams import service as teams_service
    from app.teams.service import TeamError

    fs = await service.get_settings(session)
    try:
        resolved = (
            await teams_service.get_team(session, team) if team
            else (await session.get(Team, fs.team_id) if fs.team_id else None)
        )
    except TeamError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    sharepoint = request.app.state.sharepoint
    people: list = []
    scope = "mine"
    try:
        if resolved is not None and (
            await oversight(session, user=user, roles=roles, team_id=resolved.id)
        ).may_see:
            scope = "team"
            members = await resolve_members(session, resolved, sharepoint)
            rows, _, _ = await request.app.state.team_tasks_cache.rows(
                sharepoint, team_id=resolved.id, members=members, limit=500, refresh=refresh
            )
            by_id = {u.id: u for u, _ in await teams_service.list_members(session, resolved.id)}
            people = [
                (by_id[m.user_id], rows.get(m.lookup_id or "", []))
                for m in members if m.user_id in by_id
            ]
        else:
            lookup = await sharepoint.lookup_id_for(user.email)
            if lookup is not None:
                people = [(user, await sharepoint.tasks_assigned_to(lookup, limit=500))]
    except Exception as exc:  # noqa: BLE001 - the list is unreachable, say so
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Could not read the Proposals list."
        ) from exc

    now = datetime.now(UTC)
    start, end = service.today_bounds(now)
    asked = {
        (r.task_id, r.due_at): r
        for r in (
            await session.scalars(
                select(TaskFollowup).where(TaskFollowup.due_at >= start, TaskFollowup.due_at < end)
            )
        ).all()
    }
    return DueTodayOut(
        team_name=resolved.name if (resolved and scope == "team") else None,
        scope=scope,
        grace_minutes=fs.grace_minutes,
        generated_at=now,
        tasks=service.due_today_rows(
            people, now=now, grace_minutes=fs.grace_minutes, asked=asked
        ),
    )


# ── one follow-up ──────────────────────────────────────────────────────


async def _visible(
    followup_id: uuid.UUID, user: User, roles: set[str], session: AsyncSession
) -> TaskFollowup:
    try:
        row = await service.get(session, followup_id)
    except service.FollowupError as exc:
        raise _translate(exc) from exc
    if not await service.may_see(session, row, user=user, roles=roles):
        # 404, not 403: whose tasks ran late is not something to confirm to
        # somebody with no business knowing.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="There is no such follow-up.")
    return row


@router.get("/{followup_id}", response_model=FollowupOut, summary="One follow-up")
async def read_followup(
    followup_id: uuid.UUID, user: CurrentUser, roles: CurrentRoles, session: Session
) -> FollowupOut:
    return _out(await _visible(followup_id, user, roles, session), user)


@router.post("/{followup_id}/reason", response_model=FollowupOut, summary="Say why")
async def give_reason(
    followup_id: uuid.UUID,
    body: ReasonIn,
    user: CurrentUser,
    roles: CurrentRoles,
    session: Session,
    request: Request,
    config: Config,
) -> FollowupOut:
    row = await _visible(followup_id, user, roles, session)
    try:
        await service.answer(
            session, row,
            user=user,
            reason=body.reason,
            settings=config,
            followup_settings=await service.get_settings(session),
            mailer=_mailer(request),
        )
    except service.FollowupError as exc:
        raise _translate(exc) from exc
    await session.commit()
    await session.refresh(row)
    return _out(row, user)


@router.post(
    "/{followup_id}/false-positive",
    response_model=FollowupOut,
    summary="The task was already dealt with",
)
async def false_positive(
    followup_id: uuid.UUID,
    body: FalsePositiveIn,
    user: CurrentUser,
    roles: CurrentRoles,
    session: Session,
) -> FollowupOut:
    row = await _visible(followup_id, user, roles, session)
    try:
        await service.mark_false_positive(session, row, user=user, note=body.note)
    except service.FollowupError as exc:
        raise _translate(exc) from exc
    await session.commit()
    await session.refresh(row)
    return _out(row, user)
