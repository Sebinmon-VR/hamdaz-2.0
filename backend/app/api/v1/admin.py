"""Admin panel API (§5.1) — teams, members, roles and labels."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentPrincipal, DbDep, require
from app.core.principal import Principal
from app.core.rbac import Scope
from app.models.labels import LabelKind
from app.schemas.common import Message, Page, Pagination, pagination
from app.services import label_service, team_service

router = APIRouter(prefix="/admin", tags=["admin"])

PaginationDep = Annotated[Pagination, Depends(pagination)]


# ── teams ──────────────────────────────────────────────────────────────


class TeamOut(BaseModel):
    id: str
    slug: str
    name: str
    description: str | None = None
    lead_user_id: str | None = None
    enabled_modules: list[str] = Field(default_factory=list)
    archived: bool = False
    member_count: int | None = None


class TeamCreate(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    slug: str | None = Field(default=None, max_length=64)
    description: str | None = None
    enabled_modules: list[str] = Field(default_factory=list)


class TeamUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=200)
    description: str | None = None
    lead_user_id: uuid.UUID | None = None
    enabled_modules: list[str] | None = None
    settings: dict[str, Any] | None = None


def _team_out(team: Any, member_count: int | None = None) -> TeamOut:
    return TeamOut(
        id=str(team.id),
        slug=team.slug,
        name=team.name,
        description=team.description,
        lead_user_id=str(team.lead_user_id) if team.lead_user_id else None,
        enabled_modules=list(team.enabled_modules or []),
        archived=team.archived_at is not None,
        member_count=member_count,
    )


@router.get("/teams", response_model=Page[TeamOut])
async def list_teams(
    session: DbDep,
    principal: CurrentPrincipal,
    page: PaginationDep,
    include_archived: Annotated[bool, Query()] = False,
) -> Page[TeamOut]:
    """Every team the caller can see.

    A super admin sees all of them; anyone else sees only their own. There is no permission
    check to bypass here because the filter *is* the answer.
    """
    teams, total = await team_service.list_teams(
        session, include_archived=include_archived, limit=page.limit, offset=page.offset
    )
    if not principal.is_super_admin:
        teams = [t for t in teams if t.id in principal.teams]
        total = len(teams)

    return Page.of([_team_out(t) for t in teams], total=total, limit=page.limit, offset=page.offset)


@router.post(
    "/teams",
    response_model=TeamOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require("admin.teams.manage", Scope.ALL))],
)
async def create_team(
    body: TeamCreate, session: DbDep, principal: CurrentPrincipal
) -> TeamOut:
    team = await team_service.create_team(
        session,
        actor=principal,
        name=body.name,
        slug=body.slug,
        description=body.description,
        enabled_modules=body.enabled_modules,
    )
    return _team_out(team)


@router.get("/teams/modules", response_model=list[str])
async def available_modules() -> list[str]:
    return list(team_service.AVAILABLE_MODULES)


@router.get("/teams/{team_id}", response_model=TeamOut)
async def get_team(
    team_id: uuid.UUID,
    session: DbDep,
    _: Annotated[Principal, Depends(require("proposals.read", Scope.TEAM))],
) -> TeamOut:
    team = await team_service.get_team(session, team_id)
    _members, count = await team_service.list_members(session, team_id, limit=1)
    return _team_out(team, member_count=count)


@router.patch(
    "/teams/{team_id}",
    response_model=TeamOut,
    dependencies=[Depends(require("admin.teams.manage", Scope.ALL))],
)
async def update_team(
    team_id: uuid.UUID, body: TeamUpdate, session: DbDep, principal: CurrentPrincipal
) -> TeamOut:
    team = await team_service.update_team(
        session,
        actor=principal,
        team_id=team_id,
        changes=body.model_dump(exclude_unset=True),
    )
    return _team_out(team)


@router.delete(
    "/teams/{team_id}",
    response_model=Message,
    dependencies=[Depends(require("admin.teams.manage", Scope.ALL))],
)
async def archive_team(
    team_id: uuid.UUID, session: DbDep, principal: CurrentPrincipal
) -> Message:
    """Archive, never delete — proposals and audit rows still reference the team."""
    team = await team_service.archive_team(session, actor=principal, team_id=team_id)
    return Message(message=f"{team.name} has been archived.")


# ── members ────────────────────────────────────────────────────────────


class MemberOut(BaseModel):
    user_id: str
    email: str
    display_name: str
    status: str
    role: str
    role_name: str
    labels: list[str] = Field(default_factory=list)
    joined_at: str | None = None


class MemberAdd(BaseModel):
    user_id: uuid.UUID
    role_key: str


class MemberRoleChange(BaseModel):
    role_key: str


@router.get(
    "/teams/{team_id}/members",
    response_model=Page[MemberOut],
    dependencies=[Depends(require("proposals.read", Scope.TEAM))],
)
async def list_members(
    team_id: uuid.UUID, session: DbDep, page: PaginationDep
) -> Page[MemberOut]:
    members, total = await team_service.list_members(
        session, team_id, limit=page.limit, offset=page.offset
    )

    items: list[MemberOut] = []
    for membership in members:
        labels = await label_service.active_labels_for_user(
            session, membership.user_id, team_id=team_id
        )
        items.append(
            MemberOut(
                user_id=str(membership.user_id),
                email=membership.user.email,
                display_name=membership.user.display_name,
                status=membership.user.status.value,
                role=membership.role.key,
                role_name=membership.role.name,
                labels=sorted(labels),
                joined_at=membership.joined_at.isoformat() if membership.joined_at else None,
            )
        )

    return Page.of(items, total=total, limit=page.limit, offset=page.offset)


@router.post(
    "/teams/{team_id}/members",
    response_model=Message,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require("admin.users.manage", Scope.ALL))],
)
async def add_member(
    team_id: uuid.UUID, body: MemberAdd, session: DbDep, principal: CurrentPrincipal
) -> Message:
    await team_service.add_member(
        session,
        actor=principal,
        team_id=team_id,
        user_id=body.user_id,
        role_key=body.role_key,
    )
    return Message(message="Member added.")


@router.patch(
    "/teams/{team_id}/members/{user_id}",
    response_model=Message,
    dependencies=[Depends(require("admin.users.manage", Scope.ALL))],
)
async def change_member_role(
    team_id: uuid.UUID,
    user_id: uuid.UUID,
    body: MemberRoleChange,
    session: DbDep,
    principal: CurrentPrincipal,
) -> Message:
    await team_service.change_member_role(
        session, actor=principal, team_id=team_id, user_id=user_id, role_key=body.role_key
    )
    return Message(message=f"Role changed to {body.role_key}.")


@router.delete(
    "/teams/{team_id}/members/{user_id}",
    response_model=Message,
    dependencies=[Depends(require("admin.users.manage", Scope.ALL))],
)
async def remove_member(
    team_id: uuid.UUID, user_id: uuid.UUID, session: DbDep, principal: CurrentPrincipal
) -> Message:
    await team_service.remove_member(
        session, actor=principal, team_id=team_id, user_id=user_id
    )
    return Message(message="Member removed from the team.")


# ── custom roles ───────────────────────────────────────────────────────


class RoleCreate(BaseModel):
    key: str = Field(min_length=2, max_length=64)
    name: str = Field(min_length=2, max_length=120)
    description: str | None = None
    team_id: uuid.UUID | None = None
    grants: dict[str, str] = Field(
        description="permission key -> scope (own | team | all)"
    )


class RoleGrantsUpdate(BaseModel):
    grants: dict[str, str]


class RoleOut(BaseModel):
    id: str
    key: str
    name: str
    description: str | None = None
    is_system: bool
    is_team_scoped: bool
    team_id: str | None = None
    grants: dict[str, str] = Field(default_factory=dict)
    holder_count: int = 0


@router.post(
    "/roles",
    response_model=RoleOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require("admin.roles.manage", Scope.ALL))],
)
async def create_role(
    body: RoleCreate, session: DbDep, principal: CurrentPrincipal
) -> RoleOut:
    """Compose a custom role from the permission registry.

    Every grant is validated against the registry, so the UI cannot invent a permission the
    backend does not enforce.
    """
    role = await team_service.create_custom_role(
        session,
        actor=principal,
        key=body.key,
        name=body.name,
        description=body.description,
        grants=body.grants,
        team_id=body.team_id,
    )
    return RoleOut(
        id=str(role.id),
        key=role.key,
        name=role.name,
        description=role.description,
        is_system=role.is_system,
        is_team_scoped=role.is_team_scoped,
        team_id=str(role.team_id) if role.team_id else None,
        grants=body.grants,
    )


@router.put(
    "/roles/{role_id}/grants",
    response_model=Message,
    dependencies=[Depends(require("admin.roles.manage", Scope.ALL))],
)
async def update_role_grants(
    role_id: uuid.UUID, body: RoleGrantsUpdate, session: DbDep, principal: CurrentPrincipal
) -> Message:
    role = await team_service.update_role_grants(
        session, actor=principal, role_id=role_id, grants=body.grants
    )
    holders = await team_service.count_role_holders(session, role_id)
    return Message(
        message=f"Updated {role.name}. {holders} member(s) are affected immediately."
    )


@router.get(
    "/roles/{role_id}/holders",
    response_model=Message,
    dependencies=[Depends(require("admin.roles.manage", Scope.ALL))],
)
async def role_holders(role_id: uuid.UUID, session: DbDep) -> Message:
    """How many people hold a role — shown before an edit, so the blast radius is visible."""
    count = await team_service.count_role_holders(session, role_id)
    return Message(message=f"{count} member(s) currently hold this role.")


# ── labels ─────────────────────────────────────────────────────────────


class LabelOut(BaseModel):
    id: str
    key: str
    name: str
    kind: str
    description: str | None = None
    color: str | None = None
    team_id: str | None = None


class LabelCreate(BaseModel):
    key: str = Field(min_length=2, max_length=64)
    name: str = Field(min_length=2, max_length=120)
    kind: LabelKind
    description: str | None = None
    color: str | None = None
    team_id: uuid.UUID | None = None


class LabelAssign(BaseModel):
    user_id: uuid.UUID
    label_key: str
    team_id: uuid.UUID | None = None
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


@router.get("/labels", response_model=list[LabelOut])
async def list_labels(
    session: DbDep,
    _: Annotated[Principal, Depends(require("labels.read", Scope.TEAM))],
    team_id: Annotated[uuid.UUID | None, Query()] = None,
) -> list[LabelOut]:
    labels = await label_service.list_labels(session, team_id=team_id)
    return [
        LabelOut(
            id=str(label.id),
            key=label.key,
            name=label.name,
            kind=label.kind.value,
            description=label.description,
            color=label.color,
            team_id=str(label.team_id) if label.team_id else None,
        )
        for label in labels
    ]


@router.post(
    "/labels",
    response_model=LabelOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require("labels.manage", Scope.ALL))],
)
async def create_label(
    body: LabelCreate, session: DbDep, principal: CurrentPrincipal
) -> LabelOut:
    label = await label_service.create_label(
        session,
        actor=principal,
        key=body.key,
        name=body.name,
        kind=body.kind,
        description=body.description,
        color=body.color,
        team_id=body.team_id,
    )
    return LabelOut(
        id=str(label.id),
        key=label.key,
        name=label.name,
        kind=label.kind.value,
        description=label.description,
        color=label.color,
        team_id=str(label.team_id) if label.team_id else None,
    )


@router.delete(
    "/labels/{label_id}",
    response_model=Message,
    dependencies=[Depends(require("labels.manage", Scope.ALL))],
)
async def delete_label(
    label_id: uuid.UUID, session: DbDep, principal: CurrentPrincipal
) -> Message:
    await label_service.delete_label(session, actor=principal, label_id=label_id)
    return Message(message="Label deleted.")


@router.post("/labels/assign", response_model=Message)
async def assign_label(
    body: LabelAssign,
    session: DbDep,
    principal: CurrentPrincipal,
    _: Annotated[Principal, Depends(require("labels.assign", Scope.TEAM))],
) -> Message:
    """Grant a label to a user. Re-granting refreshes the expiry rather than duplicating."""
    await label_service.assign_label(
        session,
        actor=principal,
        user_id=body.user_id,
        label_key=body.label_key,
        team_id=body.team_id,
        expires_in_days=body.expires_in_days,
    )
    return Message(message=f"Label {body.label_key!r} assigned.")


@router.delete("/labels/assign", response_model=Message)
async def revoke_label(
    body: LabelAssign,
    session: DbDep,
    principal: CurrentPrincipal,
    _: Annotated[Principal, Depends(require("labels.assign", Scope.TEAM))],
) -> Message:
    removed = await label_service.revoke_label(
        session,
        actor=principal,
        user_id=body.user_id,
        label_key=body.label_key,
        team_id=body.team_id,
    )
    return Message(
        message="Label revoked." if removed else "That user did not hold the label."
    )
