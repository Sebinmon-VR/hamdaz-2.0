"""Permission guards.

Every check answers one question: does the caller hold one of these global roles?
Team-scoped authority is the team module's business and is not decided here.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Annotated

from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import CurrentUser
from app.core.db import get_session
from app.models.user import User
from app.roles.catalogue import ADMIN_ROLES
from app.roles.service import global_role_keys


async def current_roles(
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> set[str]:
    return await global_role_keys(session, user.id)


CurrentRoles = Annotated[set[str], Depends(current_roles)]


def require_roles(*allowed: str):
    """Build a dependency that admits only holders of one of ``allowed``."""
    permitted = frozenset(allowed)

    async def guard(user: CurrentUser, roles: CurrentRoles) -> User:
        if permitted.isdisjoint(roles):
            # 403, not 404: the caller is authenticated and the resource exists.
            # Hiding that would only make the API harder to use, not safer.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires one of: {', '.join(sorted(permitted))}",
            )
        return user

    return guard


def require_admin():
    """Super admin, CEO or manager — who may manage teams, roles and people."""
    return require_roles(*ADMIN_ROLES)


#: The caller must be an admin. Yields the acting user, for audit fields.
AdminUser = Annotated[User, Depends(require_admin())]


def has_any(roles: Iterable[str], allowed: Iterable[str]) -> bool:
    return not frozenset(allowed).isdisjoint(set(roles))
