"""Which modules and pages each team can reach.

The model is allow-list: a team sees nothing until it is granted something. That
is the safe default — a new module does not silently appear for everyone the
moment it ships.

A grant is per module. Within a granted module it is either the whole thing
(``all_pages``) or an explicit subset, held in ``team_page_access``. Storing the
"whole module" case as a flag rather than as one row per page means adding a page
to an existing module does not require back-filling every team that already had
the module.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Timestamped, UUIDPrimaryKey


class Module(Base, Timestamped):
    """Mirror of the code catalogue, so grants can carry a foreign key."""

    __tablename__ = "modules"

    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    #: Reached through a global admin role, never through a team grant.
    admin_only: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    pages: Mapped[list[ModulePage]] = relationship(
        back_populates="module", cascade="all, delete-orphan", lazy="selectin"
    )

    def __repr__(self) -> str:
        return f"<Module {self.key}>"


class ModulePage(Base, UUIDPrimaryKey, Timestamped):
    __tablename__ = "module_pages"
    __table_args__ = (UniqueConstraint("module_key", "key", name="uq_module_page_key"),)

    module_key: Mapped[str] = mapped_column(
        String(40), ForeignKey("modules.key", ondelete="CASCADE"), nullable=False, index=True
    )
    key: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    #: The frontend route this page lives at.
    path: Mapped[str] = mapped_column(String(200), nullable=False)
    #: Rendered inside a team's context rather than standalone.
    team_scoped: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    module: Mapped[Module] = relationship(back_populates="pages")

    def __repr__(self) -> str:
        return f"<ModulePage {self.module_key}.{self.key}>"


class TeamModuleAccess(Base, UUIDPrimaryKey, Timestamped):
    """One team's grant of one module."""

    __tablename__ = "team_module_access"
    __table_args__ = (
        UniqueConstraint("team_id", "module_key", name="uq_team_module"),
    )

    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True
    )
    module_key: Mapped[str] = mapped_column(
        String(40), ForeignKey("modules.key", ondelete="CASCADE"), nullable=False, index=True
    )
    #: True: every page, including ones added later. False: only the rows in
    #: team_page_access.
    all_pages: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    granted_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    module: Mapped[Module] = relationship(lazy="joined")

    def __repr__(self) -> str:
        return f"<TeamModuleAccess team={self.team_id} module={self.module_key}>"


class TeamPageAccess(Base, UUIDPrimaryKey, Timestamped):
    """A single page a team may reach, when its module grant is not all_pages."""

    __tablename__ = "team_page_access"
    __table_args__ = (UniqueConstraint("team_id", "page_id", name="uq_team_page"),)

    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True
    )
    page_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("module_pages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    page: Mapped[ModulePage] = relationship(lazy="joined")

    def __repr__(self) -> str:
        return f"<TeamPageAccess team={self.team_id} page={self.page_id}>"
