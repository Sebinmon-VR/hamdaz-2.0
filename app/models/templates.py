"""Form templates: what a form asks for, as data an admin edits.

The problem this solves is that a form's fields were about to be hardcoded in
the quoting module, which means every change to what presales must fill in is a
developer and a deploy. Here the field list is a row.

**Only a super admin creates one.** A template decides what the business
records, so it is not something a team quietly changes for itself. Everybody
*uses* them, subject to the grants below.

Two axes of access, and they are different questions:

* **which team** may use a template — a purchase form is not an HR form;
* **what standing** is needed inside that team — some forms are for anyone,
  some only for a manager.

A grant with no team applies everywhere, and a grant with no roles admits anyone
on the granted team. Absence means "unrestricted on that axis", not "nobody",
because the alternative is a template that exists and nobody can use.

Templates are **archived, not deleted**, once anything has been filled in from
one: a form somebody submitted last month is unreadable if the definition behind
it has vanished.
"""

from __future__ import annotations

import uuid
from enum import StrEnum

from sqlalchemy import (
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Timestamped, UUIDPrimaryKey
from app.models.team import Team
from app.models.user import User


class TemplateStatus(StrEnum):
    #: Being written. Usable only by a super admin, so a half-finished form
    #: cannot be filled in by somebody who then loses the work.
    DRAFT = "draft"
    ACTIVE = "active"
    #: Retired but readable, so records made from it still make sense.
    ARCHIVED = "archived"


class FieldType(StrEnum):
    """What a field asks for. A frontend renders from this and nothing else."""

    TEXT = "text"
    TEXTAREA = "textarea"
    NUMBER = "number"
    CURRENCY = "currency"
    PERCENT = "percent"
    DATE = "date"
    CHECKBOX = "checkbox"
    SELECT = "select"
    #: Repeating rows — line items. Its ``columns`` are field specs in turn.
    TABLE = "table"
    #: File upload, e.g. the supplier quotes behind a price.
    FILE = "file"


class FormTemplate(Base, UUIDPrimaryKey, Timestamped):
    __tablename__ = "form_templates"
    __table_args__ = (
        UniqueConstraint("key", "version", name="uq_form_template_key_version"),
        Index("ix_form_templates_kind_status", "kind", "status"),
    )

    #: Stable identifier the code refers to, e.g. ``quote_request``.
    key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    #: What the template is *for*, so a module can find its own without knowing
    #: which particular template an admin made active.
    kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    status: Mapped[TemplateStatus] = mapped_column(
        String(16), default=TemplateStatus.DRAFT, nullable=False, index=True
    )
    #: Bumped on publish. A new version is a new row, so a record can always be
    #: read against the definition it was actually filled in against.
    version: Mapped[int] = mapped_column(
        Integer, default=1, server_default=text("1"), nullable=False
    )

    #: The fields, in order. See ``app.forms.catalogue`` for the shape.
    fields: Mapped[list] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    #: Section headings in display order, so a form can be grouped without the
    #: order being inferred from whichever field happens to come first.
    sections: Mapped[list] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )

    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    created_by: Mapped[User | None] = relationship(
        foreign_keys=[created_by_id], lazy="joined"
    )
    grants: Mapped[list[TemplateGrant]] = relationship(
        back_populates="template", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def is_usable(self) -> bool:
        return self.status == TemplateStatus.ACTIVE

    def __repr__(self) -> str:
        return f"<FormTemplate {self.key} v{self.version} {self.status}>"


class TemplateGrant(Base, UUIDPrimaryKey, Timestamped):
    """Who may use a template.

    Both fields are optional and mean "no restriction on this axis":

    * ``team_id`` null — any team;
    * ``allowed_roles`` empty — anyone on the team the grant covers.

    A template with **no grants at all** is usable by nobody but a super admin.
    That is the safe default for something just created: a form appears when
    somebody decides it should, not the moment it is saved.
    """

    __tablename__ = "template_grants"
    __table_args__ = (
        UniqueConstraint("template_id", "team_id", name="uq_template_grant_team"),
    )

    template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("form_templates.id", ondelete="CASCADE"), nullable=False
    )
    #: NULL means every team.
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), index=True
    )
    #: Role keys, global or team-scoped. Empty means anyone on that team.
    allowed_roles: Mapped[list] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    note: Mapped[str | None] = mapped_column(Text)

    template: Mapped[FormTemplate] = relationship(back_populates="grants")
    team: Mapped[Team | None] = relationship(lazy="joined")

    def __repr__(self) -> str:
        scope = "all teams" if self.team_id is None else f"team={self.team_id}"
        return f"<TemplateGrant {scope} roles={self.allowed_roles}>"
