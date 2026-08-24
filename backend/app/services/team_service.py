"""Teams, memberships and roles — the admin panel's core operations (§5.1).

Every mutation here writes an audit row in the same transaction, so the audit log can never
disagree with reality.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.principal import Principal
from app.core.rbac import SYSTEM_ROLES_BY_KEY, Scope, get_permission
from app.models.identity import Membership, Role, RolePermission, Team, User, UserStatus
from app.models.platform import AuditAction
from app.services import audit_service

TEAM_FIELDS = ("slug", "name", "description", "lead_user_id", "enabled_modules", "settings")
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

#: Modules a team can switch on. Mirrors the module inventory in the plan's §2.4.
AVAILABLE_MODULES: tuple[str, ...] = (
    "proposals",
    "quotes",
    "vendors",
    "contacts",
    "mail",
    "leave",
    "reports",
)


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "team"


# ── teams ──────────────────────────────────────────────────────────────


async def get_team(session: AsyncSession, team_id: uuid.UUID) -> Team:
    team = await session.scalar(select(Team).where(Team.id == team_id))
    if team is None:
        raise NotFoundError("That team does not exist.")
    return team


async def list_teams(
    session: AsyncSession,
    *,
    include_archived: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Team], int]:
    query = select(Team)
    if not include_archived:
        query = query.where(Team.archived_at.is_(None))

    total = await session.scalar(
        select(func.count()).select_from(query.subquery())
    )
    rows = (
        await session.scalars(query.order_by(Team.name).limit(limit).offset(offset))
    ).all()
    return list(rows), int(total or 0)


async def create_team(
    session: AsyncSession,
    *,
    actor: Principal,
    name: str,
    slug: str | None = None,
    description: str | None = None,
    enabled_modules: list[str] | None = None,
) -> Team:
    resolved_slug = (slug or slugify(name)).lower()
    if not _SLUG_RE.match(resolved_slug):
        raise ValidationError(
            "A team slug must be lowercase letters, numbers and single hyphens."
        )

    if await session.scalar(select(Team).where(Team.slug == resolved_slug)):
        raise ConflictError(f"A team with the slug {resolved_slug!r} already exists.")

    modules = _validate_modules(enabled_modules)

    team = Team(
        slug=resolved_slug,
        name=name.strip(),
        description=description,
        enabled_modules=modules,
        settings={},
    )
    session.add(team)
    await session.flush()

    await audit_service.record(
        session,
        action=AuditAction.CREATE,
        entity_type="team",
        entity_id=team.id,
        actor=actor,
        team_id=team.id,
        after=audit_service.snapshot(team, TEAM_FIELDS),
    )
    return team


async def update_team(
    session: AsyncSession, *, actor: Principal, team_id: uuid.UUID, changes: dict[str, Any]
) -> Team:
    team = await get_team(session, team_id)
    before = audit_service.snapshot(team, TEAM_FIELDS)

    if "enabled_modules" in changes:
        changes["enabled_modules"] = _validate_modules(changes["enabled_modules"])

    if "lead_user_id" in changes and changes["lead_user_id"] is not None:
        lead_id = changes["lead_user_id"]
        if not await session.scalar(
            select(Membership).where(
                Membership.team_id == team_id, Membership.user_id == lead_id
            )
        ):
            raise ValidationError("A team lead must be a member of that team.")

    for field, value in changes.items():
        if field in TEAM_FIELDS and value is not None:
            setattr(team, field, value)

    await session.flush()

    after = audit_service.snapshot(team, TEAM_FIELDS)
    await audit_service.record(
        session,
        action=AuditAction.UPDATE,
        entity_type="team",
        entity_id=team.id,
        actor=actor,
        team_id=team.id,
        before=before,
        after=audit_service.diff(before, after),
    )
    return team


async def archive_team(
    session: AsyncSession, *, actor: Principal, team_id: uuid.UUID
) -> Team:
    """Archive rather than delete: proposals and audit rows still reference the team."""
    team = await get_team(session, team_id)
    if team.archived_at is not None:
        return team

    team.archived_at = datetime.now(UTC)
    await session.flush()

    await audit_service.record(
        session,
        action=AuditAction.UPDATE,
        entity_type="team",
        entity_id=team.id,
        actor=actor,
        team_id=team.id,
        after={"archived": True},
    )
    return team


def _validate_modules(modules: list[str] | None) -> list[str]:
    if not modules:
        return []
    unknown = set(modules) - set(AVAILABLE_MODULES)
    if unknown:
        raise ValidationError(
            f"Unknown module(s): {', '.join(sorted(unknown))}. "
            f"Available: {', '.join(AVAILABLE_MODULES)}"
        )
    return sorted(set(modules))


# ── memberships ────────────────────────────────────────────────────────


async def list_members(
    session: AsyncSession, team_id: uuid.UUID, *, limit: int = 50, offset: int = 0
) -> tuple[list[Membership], int]:
    query = select(Membership).where(Membership.team_id == team_id)
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    rows = (
        await session.scalars(
            query.options(selectinload(Membership.user), selectinload(Membership.role))
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return list(rows), int(total or 0)


async def add_member(
    session: AsyncSession,
    *,
    actor: Principal,
    team_id: uuid.UUID,
    user_id: uuid.UUID,
    role_key: str,
) -> Membership:
    team = await get_team(session, team_id)
    if team.archived_at is not None:
        raise ValidationError("That team is archived.")

    user = await session.scalar(select(User).where(User.id == user_id))
    if user is None:
        raise NotFoundError("That user does not exist.")

    role = await _resolve_role(session, role_key, team_id)

    existing = await session.scalar(
        select(Membership).where(
            Membership.team_id == team_id, Membership.user_id == user_id
        )
    )
    if existing is not None:
        raise ConflictError(
            f"{user.display_name} is already in this team. Change their role instead."
        )

    membership = Membership(
        user_id=user_id, team_id=team_id, role_id=role.id, joined_at=datetime.now(UTC)
    )
    session.add(membership)

    # Joining a team is what activates an invited account. Signing in never does.
    if user.status is UserStatus.INVITED:
        user.status = UserStatus.ACTIVE

    await session.flush()

    await audit_service.record(
        session,
        action=AuditAction.CREATE,
        entity_type="membership",
        entity_id=membership.id,
        actor=actor,
        team_id=team_id,
        after={"user_id": str(user_id), "role": role.key, "email": user.email},
    )
    return membership


async def change_member_role(
    session: AsyncSession,
    *,
    actor: Principal,
    team_id: uuid.UUID,
    user_id: uuid.UUID,
    role_key: str,
) -> Membership:
    membership = await session.scalar(
        select(Membership)
        .where(Membership.team_id == team_id, Membership.user_id == user_id)
        .options(selectinload(Membership.role))
    )
    if membership is None:
        raise NotFoundError("That user is not a member of this team.")

    old_role = membership.role.key
    role = await _resolve_role(session, role_key, team_id)

    if role.id == membership.role_id:
        return membership

    await _guard_last_super_admin(session, membership, new_role=role)

    membership.role_id = role.id
    await session.flush()

    await audit_service.record(
        session,
        action=AuditAction.UPDATE,
        entity_type="membership",
        entity_id=membership.id,
        actor=actor,
        team_id=team_id,
        before={"role": old_role},
        after={"role": role.key},
    )
    return membership


async def remove_member(
    session: AsyncSession, *, actor: Principal, team_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    membership = await session.scalar(
        select(Membership)
        .where(Membership.team_id == team_id, Membership.user_id == user_id)
        .options(selectinload(Membership.role))
    )
    if membership is None:
        raise NotFoundError("That user is not a member of this team.")

    await _guard_last_super_admin(session, membership, new_role=None)

    team = await get_team(session, team_id)
    if team.lead_user_id == user_id:
        team.lead_user_id = None

    await audit_service.record(
        session,
        action=AuditAction.DELETE,
        entity_type="membership",
        entity_id=membership.id,
        actor=actor,
        team_id=team_id,
        before={"user_id": str(user_id), "role": membership.role.key},
    )
    await session.delete(membership)
    await session.flush()


async def _guard_last_super_admin(
    session: AsyncSession, membership: Membership, *, new_role: Role | None
) -> None:
    """Refuse to remove the final super admin, and say why.

    Locking everyone out of the admin panel is unrecoverable without database access.
    """
    if membership.role.key != "super_admin":
        return
    if new_role is not None and new_role.key == "super_admin":
        return

    remaining = await session.scalar(
        select(func.count())
        .select_from(Membership)
        .join(Role, Role.id == Membership.role_id)
        .where(Role.key == "super_admin", Membership.id != membership.id)
    )
    if not remaining:
        raise ValidationError(
            "This is the only super admin. Promote someone else before changing this, "
            "or nobody will be able to administer the system."
        )


async def _resolve_role(session: AsyncSession, role_key: str, team_id: uuid.UUID) -> Role:
    """Prefer a team's own custom role, then fall back to the org-wide/system one."""
    role = await session.scalar(
        select(Role).where(Role.key == role_key, Role.team_id == team_id)
    )
    if role is None:
        role = await session.scalar(
            select(Role).where(Role.key == role_key, Role.team_id.is_(None))
        )
    if role is None:
        known = ", ".join(sorted(SYSTEM_ROLES_BY_KEY))
        raise ValidationError(f"Unknown role {role_key!r}. Built-in roles: {known}")
    return role


# ── custom roles ───────────────────────────────────────────────────────


async def create_custom_role(
    session: AsyncSession,
    *,
    actor: Principal,
    key: str,
    name: str,
    grants: dict[str, str],
    team_id: uuid.UUID | None = None,
    description: str | None = None,
) -> Role:
    """Compose a role from the permission registry.

    Every grant is validated against the registry, so the admin UI cannot invent a permission
    the backend does not enforce.
    """
    normalised = key.strip().lower()
    if not _SLUG_RE.match(normalised.replace("_", "-")):
        raise ValidationError("A role key must be lowercase letters, numbers, - or _.")

    if normalised in SYSTEM_ROLES_BY_KEY:
        raise ConflictError(f"{normalised!r} is a built-in role. Choose another key.")

    if await session.scalar(
        select(Role).where(Role.key == normalised, Role.team_id.is_(team_id))
    ):
        raise ConflictError(f"A role with the key {normalised!r} already exists here.")

    validated = _validate_grants(grants)

    role = Role(
        key=normalised,
        name=name.strip(),
        description=description,
        is_system=False,
        is_team_scoped=team_id is not None,
        team_id=team_id,
    )
    session.add(role)
    await session.flush()

    for permission_key, scope in validated:
        session.add(
            RolePermission(role_id=role.id, permission_key=permission_key, scope=scope.value)
        )
    await session.flush()

    await audit_service.record(
        session,
        action=AuditAction.CREATE,
        entity_type="role",
        entity_id=role.id,
        actor=actor,
        team_id=team_id,
        after={"key": role.key, "grants": {k: s.value for k, s in validated}},
    )
    return role


async def update_role_grants(
    session: AsyncSession, *, actor: Principal, role_id: uuid.UUID, grants: dict[str, str]
) -> Role:
    role = await session.scalar(
        select(Role).where(Role.id == role_id).options(selectinload(Role.permissions))
    )
    if role is None:
        raise NotFoundError("That role does not exist.")
    if role.is_system:
        raise ValidationError(
            "Built-in roles cannot be edited. Clone it into a custom role instead."
        )

    before = {rp.permission_key: rp.scope for rp in role.permissions}
    validated = _validate_grants(grants)

    for rp in list(role.permissions):
        await session.delete(rp)
    await session.flush()

    for permission_key, scope in validated:
        session.add(
            RolePermission(role_id=role.id, permission_key=permission_key, scope=scope.value)
        )
    await session.flush()

    after = {k: s.value for k, s in validated}
    await audit_service.record(
        session,
        action=AuditAction.UPDATE,
        entity_type="role",
        entity_id=role.id,
        actor=actor,
        team_id=role.team_id,
        before=before,
        after=audit_service.diff(before, after),
    )
    return role


def _validate_grants(grants: dict[str, str]) -> list[tuple[str, Scope]]:
    if not grants:
        raise ValidationError("A role must grant at least one permission.")

    validated: list[tuple[str, Scope]] = []
    for key, raw_scope in grants.items():
        permission = get_permission(key)  # raises on an unknown key
        try:
            scope = Scope(raw_scope)
        except ValueError:
            raise ValidationError(
                f"{raw_scope!r} is not a scope. Use own, team or all."
            ) from None
        if scope not in permission.scopes:
            allowed = ", ".join(s.value for s in permission.scopes)
            raise ValidationError(
                f"{key!r} cannot be granted at {scope.value!r}. Allowed: {allowed}"
            )
        validated.append((key, scope))
    return validated


async def count_role_holders(session: AsyncSession, role_id: uuid.UUID) -> int:
    """How many people hold this role — shown before an edit, so the blast radius is visible."""
    total = await session.scalar(
        select(func.count()).select_from(Membership).where(Membership.role_id == role_id)
    )
    return int(total or 0)
