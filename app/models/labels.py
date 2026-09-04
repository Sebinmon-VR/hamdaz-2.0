"""User labels — the vocabulary the assignment policy speaks in.

**Roles grant permission; labels drive policy.** Keeping them as separate axes is
deliberate: a Senior and a New Joiner can both hold ``member`` with identical
permissions and still receive very different workloads, because the assignment
policy reads their labels and not their role. Merging the two would mean giving
someone extra permissions to give them more work.

Three kinds, because they answer different questions:

* ``CATEGORY`` — seniority. Drives *how much* work someone gets.
* ``STATUS`` — a temporary state. Drives *whether* they get any.
* ``SKILL`` — a competency. Drives *which* work they are eligible for.

Two labels are never stored, because storing them would mean keeping them up to
date and something would eventually forget. They are computed at read time
instead, from data that is already correct:

* ``on-leave`` — from an approved leave request covering today. The person leaves
  the assignment pool when their leave starts and rejoins the day it ends, with
  no job to run and no window where a stale label is still shaping assignment.
* ``new-joiner`` — from ``User.joined_on`` and the policy's window.

See :mod:`app.labels.service` for where that happens.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Timestamped, UUIDPrimaryKey
from app.models.user import User


class LabelKind(StrEnum):
    #: Seniority and contract shape. Drives capacity and ratios.
    CATEGORY = "category"
    #: Temporary states. Drives eligibility.
    STATUS = "status"
    #: Competencies. Drives eligibility for particular work.
    SKILL = "skill"


class LabelSource(StrEnum):
    """How someone came to hold a label.

    Worth recording: an admin removing a label they granted is an edit, while an
    admin removing a derived one is a request for it to come back tomorrow.
    """

    MANUAL = "manual"
    #: Computed at read time — see the module docstring. Never stored.
    DERIVED = "derived"


#: Reserved keys the assignment policy relies on by name. Admins may add others
#: freely, but these three must keep their meaning.
LABEL_NEW_JOINER = "new-joiner"
LABEL_ON_LEAVE = "on-leave"
LABEL_EXCLUDED = "excluded-from-rotation"

#: Never stored on a ``label_assignments`` row; always computed.
DERIVED_KEYS = frozenset({LABEL_NEW_JOINER, LABEL_ON_LEAVE})


class Label(Base, UUIDPrimaryKey, Timestamped):
    __tablename__ = "labels"
    __table_args__ = (
        # A team may define its own "senior" without colliding with the org's.
        UniqueConstraint("team_id", "key", name="uq_labels_team_key"),
    )

    #: NULL for an org-wide label; set for one that exists only inside a team.
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), index=True
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[LabelKind] = mapped_column(String(16), nullable=False, index=True)
    #: For the UI. Not load-bearing.
    color: Mapped[str | None] = mapped_column(String(16))
    description: Mapped[str | None] = mapped_column(Text)
    #: Seeded labels the policy refers to by key. Renaming one is fine; deleting
    #: it would leave the policy pointing at nothing, so it is refused.
    is_system: Mapped[bool] = mapped_column(
        default=False, nullable=False, server_default="false"
    )

    assignments: Mapped[list[LabelAssignment]] = relationship(
        back_populates="label", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Label {self.key} {self.kind}>"


class LabelAssignment(Base, UUIDPrimaryKey, Timestamped):
    """A label held by a person, optionally inside one team and optionally expiring."""

    __tablename__ = "label_assignments"
    __table_args__ = (
        UniqueConstraint("user_id", "label_id", "team_id", name="uq_label_assignment"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    label_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("labels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: NULL means the label applies to this person everywhere. Set means it
    #: applies only when they are being considered for that team's work — a
    #: senior in presales can be a new joiner on an estimation team.
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), index=True
    )
    assigned_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    #: Set for a label that should lapse on its own — a training period, a
    #: temporary exclusion. Filtered at read time, so there is no window in which
    #: an expired label still counts and no reaper job to forget to run.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)

    user: Mapped[User] = relationship(foreign_keys=[user_id], lazy="joined")
    label: Mapped[Label] = relationship(back_populates="assignments", lazy="joined")

    def is_active(self, *, now: datetime | None = None) -> bool:
        return self.expires_at is None or self.expires_at > (now or datetime.now(UTC))

    def __repr__(self) -> str:
        return f"<LabelAssignment user={self.user_id} label={self.label_id}>"
