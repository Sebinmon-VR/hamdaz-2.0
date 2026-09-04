"""Managing the role catalogue and who holds which global role.

Read access is open to any signed-in user — knowing the CEO is the CEO is not a
secret, and the frontend needs it to render. Every write requires an admin
(super admin, CEO or manager), with one exception spelled out below: only a
super admin may grant or revoke super admin.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import CurrentUser
from app.core.db import get_session
from app.directory.provisioning import UserNotResolvableError, resolve_user
from app.directory.router import get_directory
from app.models.role import RoleScope, UserRole
from app.models.user import User
from app.roles import service
from app.roles.catalogue import ADMIN_ROLES, SUPER_ADMIN, SUPER_ADMIN_GRANTORS
from app.roles.deps import AdminUser, CurrentRoles
from app.roles.schemas import (
    AssignRole,
    GrantOut,
    MyRolesOut,
    RoleCreate,
    RoleOut,
    RoleUpdate,
    UserRolesOut,
)
from app.roles.service import RoleConflictError, RoleError, RoleNotFoundError

router = APIRouter(prefix="/roles", tags=["roles"])

Session = Annotated[AsyncSession, Depends(get_session)]


def _grants(rows: list[UserRole]) -> list[GrantOut]:
    return [
        GrantOut(
            role=RoleOut.model_validate(row.role),
            granted_at=row.created_at,
            granted_by_id=row.granted_by_id,
        )
        for row in rows
    ]


def _user_roles_out(user: User, rows: list[UserRole]) -> UserRolesOut:
    return UserRolesOut(
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        roles=_grants(rows),
        role_keys=sorted(row.role.key for row in rows),
    )


def _translate(exc: RoleError) -> HTTPException:
    if isinstance(exc, RoleNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, RoleConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


# ── the catalogue ──────────────────────────────────────────────────────


@router.get("", response_model=list[RoleOut], summary="Every role that exists")
async def list_roles(
    _: CurrentUser,
    session: Session,
    scope: Annotated[RoleScope | None, Query(description="Filter to global or team roles")] = None,
) -> list[RoleOut]:
    return [RoleOut.model_validate(r) for r in await service.list_roles(session, scope=scope)]


@router.post(
    "", response_model=RoleOut, status_code=status.HTTP_201_CREATED, summary="Create a role"
)
async def create_role(payload: RoleCreate, _: AdminUser, session: Session) -> RoleOut:
    try:
        role = await service.create_role(
            session,
            key=payload.key,
            name=payload.name,
            scope=payload.scope,
            description=payload.description,
        )
    except RoleError as exc:
        raise _translate(exc) from exc
    return RoleOut.model_validate(role)


@router.patch("/{key}", response_model=RoleOut, summary="Rename or redescribe a role")
async def update_role(key: str, payload: RoleUpdate, _: AdminUser, session: Session) -> RoleOut:
    try:
        role = await service.update_role(
            session, key, name=payload.name, description=payload.description
        )
    except RoleError as exc:
        raise _translate(exc) from exc
    return RoleOut.model_validate(role)


@router.delete("/{key}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a custom role")
async def delete_role(key: str, _: AdminUser, session: Session) -> None:
    try:
        await service.delete_role(session, key)
    except RoleError as exc:
        raise _translate(exc) from exc


# ── who holds what ─────────────────────────────────────────────────────


@router.get("/me", response_model=MyRolesOut, summary="The caller's own roles")
async def my_roles(user: CurrentUser, roles: CurrentRoles) -> MyRolesOut:
    return MyRolesOut(
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        role_keys=sorted(roles),
        is_admin=not ADMIN_ROLES.isdisjoint(roles),
        is_super_admin=SUPER_ADMIN in roles,
    )


@router.get(
    "/assignments",
    response_model=list[UserRolesOut],
    summary="Everyone holding a global role",
)
async def list_assignments(_: CurrentUser, session: Session) -> list[UserRolesOut]:
    return [_user_roles_out(u, rows) for u, rows in await service.list_assignments(session)]


@router.get(
    "/users/{user_id}", response_model=UserRolesOut, summary="One user's global roles"
)
async def user_roles(user_id: uuid.UUID, _: CurrentUser, session: Session) -> UserRolesOut:
    user = await session.get(User, user_id)
    if user is None:
        # A read must not create anything, so this accepts an Entra object id
        # only for someone already provisioned.
        user = await session.scalar(select(User).where(User.entra_object_id == str(user_id)))
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No such user. They may not have been added to the system yet.",
        )
    return _user_roles_out(user, await service.list_user_roles(session, user.id))


@router.post(
    "/users/{user_id}",
    response_model=UserRolesOut,
    status_code=status.HTTP_201_CREATED,
    summary="Grant a global role",
)
async def assign_role(
    user_id: uuid.UUID,
    payload: AssignRole,
    actor: AdminUser,
    roles: CurrentRoles,
    session: Session,
    directory: Annotated[object, Depends(get_directory)],
) -> UserRolesOut:
    # Deliberately stricter than "an admin may assign roles": a manager who could
    # grant themselves super admin would erase the distinction entirely.
    if payload.role_key == SUPER_ADMIN and SUPER_ADMIN_GRANTORS.isdisjoint(roles):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a super admin can grant super admin",
        )

    # user_id may be a local users.id or an Entra object id — an admin picking a
    # colleague out of the directory has the latter, and that person may never
    # have signed in.
    try:
        user = await resolve_user(session, directory, str(user_id))
    except UserNotResolvableError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    try:
        await service.assign_role(
            session, user_id=user.id, role_key=payload.role_key, granted_by_id=actor.id
        )
    except RoleError as exc:
        raise _translate(exc) from exc

    return _user_roles_out(user, await service.list_user_roles(session, user.id))


@router.delete(
    "/users/{user_id}/{role_key}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke a global role",
)
async def revoke_role(
    user_id: uuid.UUID,
    role_key: str,
    _: AdminUser,
    roles: CurrentRoles,
    session: Session,
) -> None:
    if role_key == SUPER_ADMIN and SUPER_ADMIN_GRANTORS.isdisjoint(roles):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a super admin can revoke super admin",
        )

    target = await session.get(User, user_id) or await session.scalar(
        select(User).where(User.entra_object_id == str(user_id))
    )
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such user")

    try:
        await service.revoke_role(session, user_id=target.id, role_key=role_key)
    except RoleError as exc:
        raise _translate(exc) from exc
