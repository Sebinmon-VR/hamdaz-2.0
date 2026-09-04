"""Module visibility: what each team can reach, and what that means for a person.

Setting visibility is **super admin only**, per the brief — deliberately
narrower than the admin role used elsewhere. A manager can run a team; deciding
which parts of the platform exist for that team is a different, rarer decision.

Reading your own access is open to any signed-in user: the frontend needs it on
every page load to build its navigation.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.access import service
from app.access.catalogue import ACCESS_ADMINS
from app.access.schemas import (
    EffectiveAccessOut,
    GrantModule,
    ModuleGrantOut,
    ModuleOut,
    PageOut,
    SetAccess,
    TeamAccessOut,
)
from app.access.service import AccessConflictError, AccessError, AccessNotFoundError
from app.auth.deps import CurrentUser
from app.core.db import get_session
from app.models.access import TeamModuleAccess
from app.models.user import User
from app.roles.deps import CurrentRoles
from app.roles.service import global_role_keys
from app.teams import service as teams_service
from app.teams.service import TeamError, TeamNotFoundError

router = APIRouter(tags=["access"])

Session = Annotated[AsyncSession, Depends(get_session)]


async def require_access_admin(user: CurrentUser, roles: CurrentRoles) -> User:
    """Only a super admin sets module visibility."""
    if ACCESS_ADMINS.isdisjoint(roles):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a super admin can change module visibility",
        )
    return user


AccessAdmin = Annotated[User, Depends(require_access_admin)]


def _translate(exc: AccessError | TeamError) -> HTTPException:
    # TeamNotFoundError comes from the teams service and is not an
    # AccessNotFoundError; without it here an unknown team returned 400.
    if isinstance(exc, AccessNotFoundError | TeamNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, AccessConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


def _pages(pages) -> list[PageOut]:
    return [
        PageOut(key=p.key, name=p.name, path=p.path, team_scoped=p.team_scoped)
        for p in sorted(pages, key=lambda p: p.sort_order)
    ]


def _grant_out(grant: TeamModuleAccess, page_ids: set[uuid.UUID]) -> ModuleGrantOut:
    module = grant.module
    visible = (
        module.pages if grant.all_pages else [p for p in module.pages if p.id in page_ids]
    )
    return ModuleGrantOut(
        module_key=module.key,
        name=module.name,
        all_pages=grant.all_pages,
        pages=_pages(visible),
        granted_at=grant.created_at,
        granted_by_id=grant.granted_by_id,
    )


# ── the catalogue ──────────────────────────────────────────────────────


@router.get("/modules", response_model=list[ModuleOut], summary="Every module and its pages")
async def list_modules(_: CurrentUser, session: Session) -> list[ModuleOut]:
    return [
        ModuleOut(
            key=m.key,
            name=m.name,
            description=m.description,
            admin_only=m.admin_only,
            pages=_pages(m.pages),
        )
        for m in await service.list_modules(session)
    ]


# ── per-team visibility ────────────────────────────────────────────────


@router.get(
    "/teams/{ref}/access", response_model=TeamAccessOut, summary="What a team can reach"
)
async def get_team_access(ref: str, _: CurrentUser, session: Session) -> TeamAccessOut:
    try:
        team = await teams_service.get_team(session, ref)
    except TeamError as exc:
        raise _translate(exc) from exc

    grants = await service.team_access(session, team.id)
    page_ids = await service.team_page_ids(session, team.id)
    return TeamAccessOut(
        team_id=team.id,
        slug=team.slug,
        name=team.name,
        modules=[_grant_out(g, page_ids) for g in grants],
    )


@router.put(
    "/teams/{ref}/access",
    response_model=TeamAccessOut,
    summary="Replace a team's whole module set",
)
async def set_team_access(
    ref: str, payload: SetAccess, actor: AccessAdmin, session: Session
) -> TeamAccessOut:
    try:
        team = await teams_service.get_team(session, ref)
        await service.set_team_access(
            session, team=team, modules=payload.modules, granted_by_id=actor.id
        )
    except (AccessError, TeamError) as exc:
        raise _translate(exc) from exc

    grants = await service.team_access(session, team.id)
    page_ids = await service.team_page_ids(session, team.id)
    return TeamAccessOut(
        team_id=team.id,
        slug=team.slug,
        name=team.name,
        modules=[_grant_out(g, page_ids) for g in grants],
    )


@router.post(
    "/teams/{ref}/access",
    response_model=TeamAccessOut,
    status_code=status.HTTP_201_CREATED,
    summary="Grant one module to a team",
)
async def grant_module(
    ref: str, payload: GrantModule, actor: AccessAdmin, session: Session
) -> TeamAccessOut:
    try:
        team = await teams_service.get_team(session, ref)
        await service.grant_module(
            session,
            team=team,
            module_key=payload.module_key,
            page_keys=payload.page_keys,
            granted_by_id=actor.id,
        )
    except (AccessError, TeamError) as exc:
        raise _translate(exc) from exc

    grants = await service.team_access(session, team.id)
    page_ids = await service.team_page_ids(session, team.id)
    return TeamAccessOut(
        team_id=team.id,
        slug=team.slug,
        name=team.name,
        modules=[_grant_out(g, page_ids) for g in grants],
    )


@router.delete(
    "/teams/{ref}/access/{module_key}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Take a module away from a team",
)
async def revoke_module(
    ref: str, module_key: str, _: AccessAdmin, session: Session
) -> None:
    try:
        team = await teams_service.get_team(session, ref)
        await service.revoke_module(session, team=team, module_key=module_key)
    except (AccessError, TeamError) as exc:
        raise _translate(exc) from exc


# ── what a person can actually see ─────────────────────────────────────


@router.get(
    "/access/me",
    response_model=EffectiveAccessOut,
    summary="What the caller can reach, across all their teams",
)
async def my_access(
    user: CurrentUser, roles: CurrentRoles, session: Session
) -> EffectiveAccessOut:
    # The endpoint a frontend calls on load to build its navigation.
    access = await service.effective_access(session, user_id=user.id, global_roles=roles)
    return EffectiveAccessOut(user_id=user.id, **access)


@router.get(
    "/access/users/{user_id}",
    response_model=EffectiveAccessOut,
    summary="What someone else can reach",
)
async def user_access(
    user_id: uuid.UUID, _: CurrentUser, session: Session
) -> EffectiveAccessOut:
    target = await session.get(User, user_id)
    if target is None:
        target = await session.scalar(
            select(User).where(User.entra_object_id == str(user_id))
        )
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such user")

    roles = await global_role_keys(session, target.id)
    access = await service.effective_access(
        session, user_id=target.id, global_roles=roles
    )
    return EffectiveAccessOut(user_id=target.id, **access)
