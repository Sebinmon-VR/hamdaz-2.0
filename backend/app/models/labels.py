"""User category labels — the vocabulary the rules engine speaks in (§5.3).

Roles grant permission; labels drive policy. Keeping them as separate axes is deliberate: a
Senior and a New Joiner can both be ``team_member`` with identical permissions and still
receive very different workloads, because the assignment policy reads their labels.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Timestamped, UUIDPrimaryKey, enum_column

if TYPE_CHECKING:
    from app.models.identity import User


class LabelKind(StrEnum):
    #: Seniority and contract shape — drives capacity, ratios, approval routing.
    CATEGORY = "category"
    #: Competencies — drives eligibility ("only security-certified people get this work").
    SKILL = "skill"
    #: Temporary states — drives eligibility and capacity.
    STATUS = "status"


#: Reserved keys the product relies on. Admins may add any others.
LABEL_EXCLUDED_FROM_ROTATION = "excluded-from-rotation"
LABEL_NEW_JOINER = "new-joiner"


class Label(Base, UUIDPrimaryKey, Timestamped):
    __tablename__ = "labels"
    __table_args__ = (UniqueConstraint("team_id", "key", name="uq_labels_team_id_key"),)

    #: NULL for org-wide labels; set for a label that only exists inside one team.
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE")
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[LabelKind] = mapped_column(
        enum_column(LabelKind, length=16), nullable=False, index=True
    )
    color: Mapped[str | None] = mapped_column(String(16))
    description: Mapped[str | None] = mapped_column(Text)

    assignments: Mapped[list[LabelAssignment]] = relationship(
        back_populates="label", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Label {self.key}>"


class LabelAssignment(Base, UUIDPrimaryKey, Timestamped):
    """A label held by a user, optionally scoped to one team and optionally expiring.

    ``expires_at`` is what makes the *joined within 90 days → New Joiner* rule work without a
    reaper job: the label simply stops applying. Filtering on expiry at read time means there
    is no window where a stale label is still influencing assignment.
    """

    __tablename__ = "label_assignments"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "label_id", "team_id", name="uq_label_assignments_user_id_label_id_team_id"
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    label_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("labels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE")
    )
    assigned_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    #: Set when a ``user.label`` rule granted this, so the admin UI can show "automatic"
    #: and so re-running the rule updates rather than duplicates.
    granted_by_rule_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(
        back_populates="label_assignments", foreign_keys=[user_id]
    )
    label: Mapped[Label] = relationship(back_populates="assignments", lazy="selectin")

    def is_active(self, *, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return True
        return self.expires_at > (now or datetime.now(UTC))

    def __repr__(self) -> str:
        return f"<LabelAssignment user={self.user_id} label={self.label_id}>"
