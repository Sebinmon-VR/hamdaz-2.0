"""Role catalogue and global grants.

The rules that matter live here rather than in the router, because they must hold
however the call arrives — HTTP, seeding, or a future CLI:

* only ``global`` roles can be granted here (a team role without a team is a lie)
* system roles cannot be deleted
* the last super admin cannot be demoted (that is how you lock everyone out)
* granting super admin requires being one
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.role import Role, RoleScope, UserRole
from app.models.team import TeamMembership
from app.models.user import User
from app.roles.catalogue import SUPER_ADMIN, SYSTEM_ROLES


class RoleError(Exception):
    """A role operation was refused. The message is safe to show a user."""


class RoleNotFoundError(RoleError):
    pass


class RoleConflictError(RoleError):
    """The request contradicts an invariant — duplicate key, last super admin, …"""


# ── catalogue ──────────────────────────────────────────────────────────


async def seed_system_roles(session: AsyncSession) -> list[Role]:
    """Create or refresh the shipped roles. Safe to run on every deploy."""
    existing = {r.key: r for r in (await session.scalars(select(Role))).all()}
    seeded: list[Role] = []

    for spec in SYSTEM_ROLES:
        role = existing.get(spec.key)
        if role is None:
            role = Role(
                key=spec.key,
                name=spec.name,
                description=spec.description,
                scope=spec.scope,
                is_system=True,
            )
            session.add(role)
        else:
            # Scope is structural — code branches on it — so it is corrected on
            # every seed. Name and description are left alone: an admin may have
            # deliberately reworded them.
            role.scope = spec.scope
            role.is_system = True
        seeded.append(role)

    await session.flush()
    return seeded


async def list_roles(session: AsyncSession, *, scope: RoleScope | None = None) -> list[Role]:
    query = select(Role).order_by(Role.scope, Role.key)
    if scope is not None:
        query = query.where(Role.scope == scope)
    return list((await session.scalars(query)).all())


async def get_role(session: AsyncSession, key: str) -> Role:
    role = await session.scalar(select(Role).where(Role.key == key))
    if role is None:
        raise RoleNotFoundError(f"No role named {key!r}")
    return role


async def create_role(
    session: AsyncSession, *, key: str, name: str, scope: RoleScope, description: str | None = None
) -> Role:
    key = key.strip().lower()
    if not key:
        raise RoleError("Role key is required")
    if await session.scalar(select(Role).where(Role.key == key)):
        raise RoleConflictError(f"A role named {key!r} already exists")

    role = Role(key=key, name=name.strip(), description=description, scope=scope, is_system=False)
    session.add(role)
    await session.flush()
    return role


async def update_role(
    session: AsyncSession, key: str, *, name: str | None = None, description: str | None = None
) -> Role:
    role = await get_role(session, key)
    if name is not None:
        role.name = name.strip()
    if description is not None:
        role.description = description
    await session.flush()
    return role


async def delete_role(session: AsyncSession, key: str) -> None:
    role = await get_role(session, key)
    if role.is_system:
        raise RoleConflictError(f"{role.key!r} is a system role and cannot be deleted")

    # Both places a role can be held: organisation-wide, and inside a team.
    # Missing the second means the delete reaches the database and trips the
    # RESTRICT on team_memberships, surfacing as a 500 instead of a clear 409.
    held_globally = await session.scalar(
        select(func.count()).select_from(UserRole).where(UserRole.role_id == role.id)
    )
    held_in_teams = await session.scalar(
        select(func.count())
        .select_from(TeamMembership)
        .where(TeamMembership.role_id == role.id)
    )
    held_by = (held_globally or 0) + (held_in_teams or 0)
    if held_by:
        # Deleting would strip those grants; make it an explicit two-step so
        # nobody loses access by accident.
        where = []
        if held_globally:
            where.append(f"{held_globally} organisation-wide")
        if held_in_teams:
            where.append(f"{held_in_teams} in teams")
        raise RoleConflictError(
            f"{role.key!r} is still held ({', '.join(where)}); revoke it first"
        )

    await session.delete(role)
    await session.flush()


# ── grants ─────────────────────────────────────────────────────────────


async def global_role_keys(session: AsyncSession, user_id: uuid.UUID) -> set[str]:
    """Every global role key a user holds. The basis of every permission check."""
    keys = await session.scalars(
        select(Role.key)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user_id)
    )
    return set(keys.all())


async def list_user_roles(session: AsyncSession, user_id: uuid.UUID) -> list[UserRole]:
    return list(
        (
            await session.scalars(
                select(UserRole)
                .where(UserRole.user_id == user_id)
                .order_by(UserRole.created_at)
            )
        ).all()
    )


async def list_assignments(session: AsyncSession) -> list[tuple[User, list[UserRole]]]:
    """Everyone who holds at least one global role, for the admin screen."""
    rows = (
        await session.scalars(
            select(UserRole).options(selectinload(UserRole.role)).order_by(UserRole.created_at)
        )
    ).all()

    by_user: dict[uuid.UUID, list[UserRole]] = {}
    for row in rows:
        by_user.setdefault(row.user_id, []).append(row)
    if not by_user:
        return []

    users = (await session.scalars(select(User).where(User.id.in_(by_user)))).all()
    ordered = sorted(users, key=lambda u: u.display_name.casefold())
    return [(u, by_user[u.id]) for u in ordered]


async def assign_role(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    role_key: str,
    granted_by_id: uuid.UUID | None,
) -> UserRole:
    user = await session.get(User, user_id)
    if user is None:
        raise RoleNotFoundError("No such user")

    role = await get_role(session, role_key)
    # scope round-trips through a plain String column, so it comes back as str.
    # RoleScope is a StrEnum, which makes this comparison correct either way.
    if role.scope != RoleScope.GLOBAL:
        raise RoleConflictError(
            f"{role.key!r} is a team role — grant it inside a team, not organisation-wide"
        )

    already = await session.scalar(
        select(UserRole).where(UserRole.user_id == user_id, UserRole.role_id == role.id)
    )
    if already is not None:
        # Idempotent: re-granting is not an error, it just changes nothing.
        return already

    grant = UserRole(user_id=user_id, role_id=role.id, granted_by_id=granted_by_id)
    session.add(grant)
    await session.flush()
    return grant


async def revoke_role(session: AsyncSession, *, user_id: uuid.UUID, role_key: str) -> None:
    role = await get_role(session, role_key)
    grant = await session.scalar(
        select(UserRole).where(UserRole.user_id == user_id, UserRole.role_id == role.id)
    )
    if grant is None:
        raise RoleNotFoundError(f"That user does not hold {role_key!r}")

    if role.key == SUPER_ADMIN:
        remaining = await session.scalar(
            select(func.count()).select_from(UserRole).where(UserRole.role_id == role.id)
        )
        if remaining <= 1:
            # Nobody left who could grant it back. Refusing is the only safe answer.
            raise RoleConflictError(
                "Cannot revoke the last super admin — grant it to someone else first"
            )

    await session.delete(grant)
    await session.flush()


async def count_super_admins(session: AsyncSession) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(UserRole)
            .join(Role, Role.id == UserRole.role_id)
            .where(Role.key == SUPER_ADMIN)
        )
        or 0
    )
