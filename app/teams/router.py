"""Teams: create, edit, archive, delete, and manage who is in them.

Per the brief, every write here requires an organisation admin — super admin,
CEO or manager. Reads are open to any signed-in user: people need to see the
org chart to work in it.

A team lead can currently *see* their team but not change its membership. That
follows the brief literally; if leads should manage their own team, the guard to
relax is ``_can_manage`` below.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import CurrentUser
from app.core.db import get_session
from app.directory.provisioning import UserNotResolvableError, resolve_user
from app.directory.router import get_directory
from app.models.team import Team, TeamMembership
from app.models.user import User
from app.roles.deps import AdminUser
from app.teams import service
from app.teams.schemas import (
    BulkAdd,
    BulkResult,
    MemberOut,
    MemberRoles,
    MemberUpsert,
    MyTeamOut,
    TeamCreate,
    TeamDetailOut,
    TeamOut,
    TeamUpdate,
)
from app.teams.service import TeamConflictError, TeamError, TeamNotFoundError

router = APIRouter(prefix="/teams", tags=["teams"])

Session = Annotated[AsyncSession, Depends(get_session)]
Directory = Annotated[object, Depends(get_directory)]


def _translate(exc: TeamError) -> HTTPException:
    if isinstance(exc, TeamNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, TeamConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


def _member_out(user: User, rows: list[TeamMembership]) -> MemberOut:
    return MemberOut(
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        entra_object_id=user.entra_object_id,
        is_active=user.is_active,
        role_keys=sorted(row.role.key for row in rows),
        joined_at=min(row.created_at for row in rows),
    )


def _team_out(team: Team, member_count: int = 0) -> TeamOut:
    out = TeamOut.model_validate(team)
    out.member_count = member_count
    return out


# ── teams ──────────────────────────────────────────────────────────────


@router.get("", response_model=list[TeamOut], summary="Every team")
async def list_teams(
    _: CurrentUser,
    session: Session,
    search: Annotated[str | None, Query(description="Match name or handle")] = None,
    include_archived: Annotated[bool, Query()] = False,
) -> list[TeamOut]:
    teams = await service.list_teams(
        session, search=search, include_archived=include_archived
    )
    counts = await service.member_counts(session, [t.id for t in teams])
    return [_team_out(t, counts.get(t.id, 0)) for t in teams]


@router.get("/me", response_model=list[MyTeamOut], summary="Teams the caller belongs to")
async def my_teams(
    user: CurrentUser,
    session: Session,
    include_archived: Annotated[bool, Query()] = False,
) -> list[MyTeamOut]:
    pairs = await service.teams_for_user(
        session, user.id, include_archived=include_archived
    )
    counts = await service.member_counts(session, [t.id for t, _ in pairs])
    return [
        MyTeamOut(team=_team_out(t, counts.get(t.id, 0)), role_keys=keys) for t, keys in pairs
    ]


@router.post(
    "", response_model=TeamOut, status_code=status.HTTP_201_CREATED, summary="Create a team"
)
async def create_team(payload: TeamCreate, actor: AdminUser, session: Session) -> TeamOut:
    try:
        team = await service.create_team(
            session,
            name=payload.name,
            description=payload.description,
            slug=payload.slug,
            created_by_id=actor.id,
        )
    except TeamError as exc:
        raise _translate(exc) from exc
    return _team_out(team, 0)


@router.get("/{ref}", response_model=TeamDetailOut, summary="One team, with its members")
async def get_team(ref: str, _: CurrentUser, session: Session) -> TeamDetailOut:
    try:
        team = await service.get_team(session, ref)
    except TeamError as exc:
        raise _translate(exc) from exc

    members = await service.list_members(session, team.id)
    return TeamDetailOut(
        **_team_out(team, len(members)).model_dump(),
        members=[_member_out(u, rows) for u, rows in members],
    )


@router.patch("/{ref}", response_model=TeamOut, summary="Rename or redescribe a team")
async def update_team(
    ref: str, payload: TeamUpdate, _: AdminUser, session: Session
) -> TeamOut:
    try:
        team = await service.update_team(
            session,
            ref,
            name=payload.name,
            description=payload.description,
            slug=payload.slug,
        )
    except TeamError as exc:
        raise _translate(exc) from exc
    counts = await service.member_counts(session, [team.id])
    return _team_out(team, counts.get(team.id, 0))


@router.post("/{ref}/archive", response_model=TeamOut, summary="Archive a team")
async def archive_team(ref: str, _: AdminUser, session: Session) -> TeamOut:
    try:
        team = await service.archive_team(session, ref)
    except TeamError as exc:
        raise _translate(exc) from exc
    counts = await service.member_counts(session, [team.id])
    return _team_out(team, counts.get(team.id, 0))


@router.post("/{ref}/restore", response_model=TeamOut, summary="Bring an archived team back")
async def restore_team(ref: str, _: AdminUser, session: Session) -> TeamOut:
    try:
        team = await service.restore_team(session, ref)
    except TeamError as exc:
        raise _translate(exc) from exc
    counts = await service.member_counts(session, [team.id])
    return _team_out(team, counts.get(team.id, 0))


@router.delete(
    "/{ref}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete an archived team"
)
async def delete_team(ref: str, _: AdminUser, session: Session) -> None:
    # Refuses unless the team is archived — one mistaken call should not be able
    # to destroy a live team and everyone's place in it.
    try:
        await service.delete_team(session, ref)
    except TeamError as exc:
        raise _translate(exc) from exc


# ── membership ─────────────────────────────────────────────────────────


@router.get("/{ref}/members", response_model=list[MemberOut], summary="Members of a team")
async def list_members(ref: str, _: CurrentUser, session: Session) -> list[MemberOut]:
    try:
        team = await service.get_team(session, ref)
    except TeamError as exc:
        raise _translate(exc) from exc
    return [_member_out(u, rows) for u, rows in await service.list_members(session, team.id)]


@router.post(
    "/{ref}/members",
    response_model=MemberOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add someone, or replace their roles in the team",
)
async def add_member(
    ref: str,
    payload: MemberUpsert,
    actor: AdminUser,
    session: Session,
    directory: Directory,
) -> MemberOut:
    try:
        team = await service.get_team(session, ref)
    except TeamError as exc:
        raise _translate(exc) from exc

    try:
        user = await resolve_user(session, directory, payload.user_id)
    except UserNotResolvableError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    try:
        rows = await service.set_member_roles(
            session,
            team=team,
            user=user,
            role_keys=payload.role_keys,
            added_by_id=actor.id,
        )
    except TeamError as exc:
        raise _translate(exc) from exc
    return _member_out(user, rows)


@router.post(
    "/{ref}/members/bulk", response_model=BulkResult, summary="Add several people at once"
)
async def add_members(
    ref: str,
    payload: BulkAdd,
    actor: AdminUser,
    session: Session,
    directory: Directory,
) -> BulkResult:
    try:
        team = await service.get_team(session, ref)
    except TeamError as exc:
        raise _translate(exc) from exc

    added: list[MemberOut] = []
    failed: list[dict[str, str]] = []

    for raw_id in payload.user_ids:
        # Each person is attempted independently: one bad id should not discard
        # the rest, but the caller must be told exactly which ones failed.
        try:
            user = await resolve_user(session, directory, raw_id)
            rows = await service.set_member_roles(
                session,
                team=team,
                user=user,
                role_keys=payload.role_keys,
                added_by_id=actor.id,
            )
            added.append(_member_out(user, rows))
        except (UserNotResolvableError, TeamError) as exc:
            failed.append({"user_id": raw_id, "reason": str(exc)})

    return BulkResult(added=added, failed=failed)


@router.patch(
    "/{ref}/members/{user_id}",
    response_model=MemberOut,
    summary="Change someone's roles within the team",
)
async def set_member_roles(
    ref: str,
    user_id: uuid.UUID,
    payload: MemberRoles,
    actor: AdminUser,
    session: Session,
) -> MemberOut:
    try:
        team = await service.get_team(session, ref)
    except TeamError as exc:
        raise _translate(exc) from exc

    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such user")
    if not await service.member_rows(session, team.id, user.id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="That user is not in this team"
        )

    try:
        rows = await service.set_member_roles(
            session, team=team, user=user, role_keys=payload.role_keys, added_by_id=actor.id
        )
    except TeamError as exc:
        raise _translate(exc) from exc
    return _member_out(user, rows)


@router.delete(
    "/{ref}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove someone from the team",
)
async def remove_member(
    ref: str, user_id: uuid.UUID, _: AdminUser, session: Session
) -> None:
    try:
        team = await service.get_team(session, ref)
        await service.remove_member(session, team=team, user_id=user_id)
    except TeamError as exc:
        raise _translate(exc) from exc


@router.get(
    "/by-user/{user_id}",
    response_model=list[MyTeamOut],
    summary="Which teams someone belongs to",
)
async def teams_for_user(
    user_id: uuid.UUID,
    _: CurrentUser,
    session: Session,
    include_archived: Annotated[bool, Query()] = False,
) -> list[MyTeamOut]:
    if await session.get(User, user_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such user")
    pairs = await service.teams_for_user(session, user_id, include_archived=include_archived)
    counts = await service.member_counts(session, [t.id for t, _ in pairs])
    return [
        MyTeamOut(team=_team_out(t, counts.get(t.id, 0)), role_keys=keys) for t, keys in pairs
    ]
