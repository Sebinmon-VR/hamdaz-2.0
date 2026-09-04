"""Roles and the global grants of them.

Two kinds of role live in one catalogue, told apart by ``scope``:

* ``global`` — authority over the organisation. super_admin, ceo, manager.
  Granted through ``user_roles``, which is what this module manages.
* ``team`` — authority *inside* a team. team_lead, member, approver. Meaningless
  without saying which team, so these are never granted here; the team module
  grants them per team. The catalogue still owns them so there is one list of
  what a role can be.

That split is enforced in the service layer, not left to convention: putting a
team role in ``user_roles`` would silently mean "team lead of everything".
"""

from __future__ import annotations

import uuid
from enum import StrEnum

from sqlalchemy import Boolean, ForeignKey, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Timestamped, UUIDPrimaryKey


class RoleScope(StrEnum):
    GLOBAL = "global"
    TEAM = "team"


class Role(Base, UUIDPrimaryKey, Timestamped):
    __tablename__ = "roles"

    #: Stable machine name used in code and permission checks. Never displayed.
    key: Mapped[str] = mapped_column(String(40), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    scope: Mapped[RoleScope] = mapped_column(String(10), nullable=False, index=True)

    #: Seeded roles the platform reasons about by name. They can be renamed and
    #: described, but not deleted — dropping super_admin would leave nobody able
    #: to grant it back.
    is_system: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )

    def __repr__(self) -> str:
        return f"<Role {self.key}>"


class UserRole(Base, UUIDPrimaryKey, Timestamped):
    """A global role held by a user."""

    __tablename__ = "user_roles"
    __table_args__ = (
        # Granting the same role twice is a no-op, not a second grant.
        UniqueConstraint("user_id", "role_id", name="uq_user_roles_user_role"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("roles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Who granted it. SET NULL rather than CASCADE: the grant must survive the
    #: granter leaving, or removing an admin would quietly strip everyone they
    #: ever promoted.
    granted_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    role: Mapped[Role] = relationship(lazy="joined")

    def __repr__(self) -> str:
        return f"<UserRole user={self.user_id} role={self.role_id}>"
