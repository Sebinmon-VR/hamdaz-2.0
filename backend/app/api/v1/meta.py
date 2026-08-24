"""Metadata endpoints — the permission registry, served to the admin UI.

§4.3 promises that one registry drives enforcement, the admin role editor and the docs. This
router is the second of those: the role editor renders its checkbox matrix from exactly the
same structure that :func:`app.api.deps.require` enforces against, so the UI cannot offer a
permission the backend does not know about.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.deps import CurrentPrincipal
from app.core.rbac import SYSTEM_ROLES, registry_by_module

router = APIRouter(prefix="/meta", tags=["meta"])


class PermissionOut(BaseModel):
    key: str
    module: str
    description: str
    scopes: list[str]


class ModuleOut(BaseModel):
    module: str
    permissions: list[PermissionOut]


class SystemRoleOut(BaseModel):
    key: str
    name: str
    description: str
    is_team_scoped: bool
    grants: dict[str, str] = Field(
        description="permission key → scope granted by this system role"
    )


@router.get("/permissions", response_model=list[ModuleOut])
async def list_permissions() -> list[ModuleOut]:
    """The full permission registry, grouped by module."""
    return [
        ModuleOut(
            module=module,
            permissions=[
                PermissionOut(
                    key=p.key,
                    module=p.module,
                    description=p.description,
                    scopes=[s.value for s in p.scopes],
                )
                for p in permissions
            ],
        )
        for module, permissions in registry_by_module().items()
    ]


@router.get("/roles", response_model=list[SystemRoleOut])
async def list_system_roles() -> list[SystemRoleOut]:
    """Built-in roles. Custom roles live in the database and are served by the admin router."""
    return [
        SystemRoleOut(
            key=role.key,
            name=role.name,
            description=role.description,
            is_team_scoped=role.is_team_scoped,
            grants={key: scope.value for key, scope in role.grants},
        )
        for role in SYSTEM_ROLES
    ]


class MeOut(BaseModel):
    user_id: str
    email: str
    display_name: str
    is_super_admin: bool
    org_permissions: dict[str, str]
    teams: list[dict[str, object]]


@router.get("/me", response_model=MeOut)
async def whoami(principal: CurrentPrincipal) -> MeOut:
    """The caller's own resolved authorization picture.

    The frontend uses this to decide what to render. It is a convenience, never a control:
    every endpoint re-checks server-side.
    """
    return MeOut(
        user_id=str(principal.user_id),
        email=principal.email,
        display_name=principal.display_name,
        is_super_admin=principal.is_super_admin,
        org_permissions={k: v.value for k, v in principal.org_permissions.items()},
        teams=[
            {
                "team_id": str(grant.team_id),
                "slug": grant.team_slug,
                "role": grant.role_key,
                "labels": sorted(principal.label_keys(grant.team_id)),
                "permissions": {k: v.value for k, v in grant.permissions.items()},
            }
            for grant in principal.teams.values()
        ],
    )
