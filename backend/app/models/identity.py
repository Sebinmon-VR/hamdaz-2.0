"""Users, teams, roles, permissions and memberships — the §4 model.

The shape here is what makes the whole rebuild work: a user belongs to *many* teams with a
*different role in each*. The legacy system could not express that, because a user's role was
a single string in a spreadsheet cell.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Timestamped, UUIDPrimaryKey, enum_column

if TYPE_CHECKING:
    from app.models.labels import LabelAssignment


class UserStatus(StrEnum):
    ACTIVE = "active"
    INVITED = "invited"
    DEACTIVATED = "deactivated"


class User(Base, UUIDPrimaryKey, Timestamped):
    __tablename__ = "users"

    #: Entra ID object ID — the stable identifier. Email can change; this cannot.
    azure_object_id: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    photo_url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[UserStatus] = mapped_column(
        enum_column(UserStatus, length=20),
        default=UserStatus.INVITED,
        nullable=False,
        index=True,
    )
    #: Drives the ``new_joiner`` auto-label rule (§5.3). Distinct from ``created_at``, which
    #: is when the row appeared here — a user imported from the legacy system joined long
    #: before Hamdaz 2.0 first saw them.
    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    memberships: Mapped[list[Membership]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )
    #: foreign_keys is required: label_assignments references users twice (the holder
    #: via user_id, and the admin who granted it via assigned_by).
    label_assignments: Mapped[list[LabelAssignment]] = relationship(
        back_populates="user",
        foreign_keys="LabelAssignment.user_id",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    @property
    def is_active(self) -> bool:
        return self.status is UserStatus.ACTIVE

    def __repr__(self) -> str:
        return f"<User {self.email}>"


class Team(Base, UUIDPrimaryKey, Timestamped):
    __tablename__ = "teams"

    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    lead_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    #: Which modules this team sees. A team only gets what it has enabled — that is what
    #: lets one platform serve teams doing unlike work.
    enabled_modules: Mapped[list[Any]] = mapped_column(JSONB, default=list, nullable=False)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    memberships: Mapped[list[Membership]] = relationship(
        back_populates="team", cascade="all, delete-orphan"
    )

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None

    def __repr__(self) -> str:
        return f"<Team {self.slug}>"


class Permission(Base):
    """Mirror of the code registry in :mod:`app.core.rbac`.

    The registry in code is authoritative; this table exists so role grants can carry a real
    foreign key and so the admin UI can join against it. It is re-seeded on every migration.
    """

    __tablename__ = "permissions"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    module: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    #: Scopes this permission supports, e.g. ``["team", "all"]``.
    scopes: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)

    def __repr__(self) -> str:
        return f"<Permission {self.key}>"


class Role(Base, UUIDPrimaryKey, Timestamped):
    __tablename__ = "roles"
    __table_args__ = (
        UniqueConstraint("team_id", "key", name="uq_roles_team_id_key"),
        Index("ix_roles_key", "key"),
    )

    #: NULL for system roles and org-wide custom roles; set for a team's own custom role.
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE")
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    #: System roles ship with the product and cannot be deleted or renamed.
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_team_scoped: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    permissions: Mapped[list[RolePermission]] = relationship(
        back_populates="role", cascade="all, delete-orphan", lazy="selectin"
    )

    def __repr__(self) -> str:
        return f"<Role {self.key}>"


class RolePermission(Base):
    __tablename__ = "role_permissions"
    __table_args__ = (
        UniqueConstraint(
            "role_id", "permission_key", name="uq_role_permissions_role_id_permission_key"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("roles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    permission_key: Mapped[str] = mapped_column(
        String(80), ForeignKey("permissions.key", ondelete="CASCADE"), nullable=False
    )
    #: ``own`` | ``team`` | ``all`` — see :class:`app.core.rbac.Scope`.
    scope: Mapped[str] = mapped_column(String(8), nullable=False)

    role: Mapped[Role] = relationship(back_populates="permissions")

    def __repr__(self) -> str:
        return f"<RolePermission {self.permission_key}@{self.scope}>"


class Membership(Base, UUIDPrimaryKey, Timestamped):
    """One user's role within one team. The join that makes multi-team possible."""

    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("user_id", "team_id", name="uq_memberships_user_id_team_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("roles.id", ondelete="RESTRICT"), nullable=False
    )
    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="memberships")
    team: Mapped[Team] = relationship(back_populates="memberships")
    role: Mapped[Role] = relationship(lazy="selectin")

    def __repr__(self) -> str:
        return f"<Membership user={self.user_id} team={self.team_id}>"
