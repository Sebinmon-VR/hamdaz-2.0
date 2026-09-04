"""Teams and the roles people hold inside them.

This is the other half of the hybrid role model. ``user_roles`` says what someone
is across the organisation; ``team_memberships`` says what they are *within one
team*. Only roles with ``scope="team"`` may appear here, which is what stops
"team lead" from silently meaning "lead of everything".

A person may hold more than one role in the same team — leading a team and
approving its work are different jobs that often land on the same person — so
the unique constraint is (team, user, role) rather than (team, user). Membership
of a team is therefore "has at least one row here".
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Timestamped, UUIDPrimaryKey
from app.models.role import Role
from app.models.user import User

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    """A URL-safe handle for a team name. ``"Site Ops (UAE)"`` → ``"site-ops-uae"``."""
    return _SLUG_STRIP.sub("-", value.strip().casefold()).strip("-")[:64]


class Team(Base, UUIDPrimaryKey, Timestamped):
    __tablename__ = "teams"

    #: Stable handle. Kept unique so a team can be addressed by name in URLs and
    #: scripts without knowing its uuid.
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    #: Archived teams keep their history and stay addressable, but drop out of
    #: normal listings. Deleting is the separate, deliberate action.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    memberships: Mapped[list[TeamMembership]] = relationship(
        back_populates="team", cascade="all, delete-orphan"
    )

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None

    def __repr__(self) -> str:
        return f"<Team {self.slug}>"


class TeamMembership(Base, UUIDPrimaryKey, Timestamped):
    __tablename__ = "team_memberships"
    __table_args__ = (
        UniqueConstraint("team_id", "user_id", "role_id", name="uq_team_member_role"),
        # Every listing is "the members of this team", so lead with team_id.
        Index("ix_team_memberships_team_user", "team_id", "user_id"),
    )

    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("roles.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    #: RESTRICT above, not CASCADE: deleting a role that people still hold in
    #: teams would silently empty those teams. The roles module already refuses
    #: to delete a held role; this is the database agreeing.

    added_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    team: Mapped[Team] = relationship(back_populates="memberships")
    user: Mapped[User] = relationship(foreign_keys=[user_id], lazy="joined")
    role: Mapped[Role] = relationship(lazy="joined")

    def __repr__(self) -> str:
        return f"<TeamMembership team={self.team_id} user={self.user_id}>"
