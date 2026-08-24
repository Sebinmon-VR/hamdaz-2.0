"""Sync the code registry into the database.

:mod:`app.core.rbac` is authoritative for permissions and system roles. This reconciles the
``permissions``, ``roles`` and ``role_permissions`` tables to match it, and is idempotent —
safe to run on every deploy.

The direction matters: code → database, never the reverse. That is what stops the permission
model drifting the way the legacy Excel role file did.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.rbac import PERMISSIONS, SYSTEM_ROLES, validate_registry
from app.models.identity import Permission, Role, RolePermission

logger = get_logger(__name__)


async def sync_permissions(session: AsyncSession) -> int:
    """Upsert every registry permission. Returns the number written."""
    existing = {p.key: p for p in (await session.scalars(select(Permission))).all()}
    written = 0

    for permission in PERMISSIONS:
        scopes = [s.value for s in permission.scopes]
        row = existing.get(permission.key)
        if row is None:
            session.add(
                Permission(
                    key=permission.key,
                    module=permission.module,
                    description=permission.description,
                    scopes=scopes,
                )
            )
            written += 1
        elif (row.module, row.description, row.scopes) != (
            permission.module,
            permission.description,
            scopes,
        ):
            row.module = permission.module
            row.description = permission.description
            row.scopes = scopes
            written += 1

    # Permissions removed from the registry are left in place deliberately: dropping one
    # would cascade to role_permissions and silently revoke access. Removal is a migration
    # a human writes, having decided what happens to the roles that still grant it.
    stale = set(existing) - {p.key for p in PERMISSIONS}
    if stale:
        logger.warning("seed.stale_permissions", keys=sorted(stale))

    await session.flush()
    return written


async def sync_system_roles(session: AsyncSession) -> int:
    """Upsert the built-in roles and reconcile their grants."""
    existing = {
        r.key: r
        for r in (await session.scalars(select(Role).where(Role.is_system.is_(True)))).all()
    }
    written = 0

    for system_role in SYSTEM_ROLES:
        role = existing.get(system_role.key)
        if role is None:
            role = Role(
                key=system_role.key,
                name=system_role.name,
                description=system_role.description,
                is_system=True,
                is_team_scoped=system_role.is_team_scoped,
                team_id=None,
            )
            session.add(role)
            await session.flush()
            written += 1
        else:
            role.name = system_role.name
            role.description = system_role.description
            role.is_team_scoped = system_role.is_team_scoped

        desired = {key: scope.value for key, scope in system_role.grants}

        # Queried explicitly rather than read off role.permissions. A freshly added role has
        # that collection unloaded, and touching it emits a lazy SELECT — which async
        # SQLAlchemy cannot do outside a greenlet, so it raises MissingGreenlet.
        current = {
            rp.permission_key: rp
            for rp in (
                await session.scalars(
                    select(RolePermission).where(RolePermission.role_id == role.id)
                )
            ).all()
        }

        for key, scope in desired.items():
            row = current.get(key)
            if row is None:
                session.add(RolePermission(role_id=role.id, permission_key=key, scope=scope))
                written += 1
            elif row.scope != scope:
                row.scope = scope
                written += 1

        # A grant removed from a system role in code is a deliberate revocation.
        for key, row in current.items():
            if key not in desired:
                await session.delete(row)
                written += 1

    await session.flush()
    return written


async def seed_all(session: AsyncSession) -> dict[str, int]:
    """Run the full reconciliation. Called from the CLI and from deploy."""
    validate_registry()

    permissions = await sync_permissions(session)
    roles = await sync_system_roles(session)

    logger.info("seed.complete", permissions_written=permissions, role_grants_written=roles)
    return {"permissions": permissions, "role_grants": roles}
