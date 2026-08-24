"""Proposals — the core business entity (§7).

``source`` and ``source_id`` map each row 1:1 back to its SharePoint list item, which is what
makes the read-only delta sync idempotent. Nothing here is ever written back: SharePoint is
live (C2).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Timestamped, UUIDPrimaryKey, enum_column


class ProposalStatus(StrEnum):
    NEW = "new"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    SUBMITTED = "submitted"
    WON = "won"
    LOST = "lost"
    CANCELLED = "cancelled"

    @property
    def is_open(self) -> bool:
        """Open work counts toward someone's load in the assignment engine."""
        return self in _OPEN_STATUSES


_OPEN_STATUSES = frozenset(
    {ProposalStatus.NEW, ProposalStatus.ASSIGNED, ProposalStatus.IN_PROGRESS}
)

OPEN_STATUS_VALUES: tuple[str, ...] = tuple(s.value for s in _OPEN_STATUSES)


class ProposalSource(StrEnum):
    SHAREPOINT = "sharepoint"
    MANUAL = "manual"
    IMPORT = "import"


class Proposal(Base, UUIDPrimaryKey, Timestamped):
    __tablename__ = "proposals"
    __table_args__ = (
        # One row per source record: what makes re-running the sync safe.
        UniqueConstraint("source", "source_id", name="uq_proposals_source_source_id"),
        Index("ix_proposals_team_status", "team_id", "status"),
        Index("ix_proposals_assigned_status", "assigned_to", "status"),
        Index("ix_proposals_bcd", "bcd"),
    )

    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Human-facing reference, e.g. the SharePoint list item's Title.
    external_ref: Mapped[str | None] = mapped_column(String(120), index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    customer_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    customer_name: Mapped[str | None] = mapped_column(String(300))

    status: Mapped[ProposalStatus] = mapped_column(
        enum_column(ProposalStatus, length=20),
        default=ProposalStatus.NEW,
        nullable=False,
        index=True,
    )
    submission_status: Mapped[str | None] = mapped_column(String(60))

    assigned_to: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    previous_owner: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: Bid closing date — drives escalation rules and the overdue view.
    bcd: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    estimated_value: Mapped[float | None] = mapped_column(Numeric(18, 2))
    currency: Mapped[str | None] = mapped_column(String(3))
    priority_score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    #: Skills the work needs; matched against candidates' labels during assignment.
    required_labels: Mapped[list[Any]] = mapped_column(JSONB, default=list, nullable=False)

    source: Mapped[ProposalSource] = mapped_column(
        enum_column(ProposalSource, length=20), default=ProposalSource.MANUAL, nullable=False
    )
    #: The SharePoint list item id, where applicable.
    source_id: Mapped[str | None] = mapped_column(String(120))
    #: Raw source fields, kept so a mapping bug is diagnosable without re-reading SharePoint.
    source_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    events: Mapped[list[ProposalEvent]] = relationship(
        back_populates="proposal", cascade="all, delete-orphan", order_by="ProposalEvent.created_at"
    )

    @property
    def is_open(self) -> bool:
        return self.status.is_open

    def __repr__(self) -> str:
        return f"<Proposal {self.external_ref or self.id} {self.status}>"


class ProposalEventType(StrEnum):
    CREATED = "created"
    ASSIGNED = "assigned"
    REASSIGNED = "reassigned"
    STATUS_CHANGED = "status_changed"
    ESCALATED = "escalated"
    SYNCED = "synced"
    COMMENT = "comment"


class ProposalEvent(Base, UUIDPrimaryKey):
    """The per-proposal timeline shown on its detail page."""

    __tablename__ = "proposal_events"
    __table_args__ = (Index("ix_proposal_events_proposal_created", "proposal_id", "created_at"),)

    proposal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("proposals.id", ondelete="CASCADE"), nullable=False
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    type: Mapped[ProposalEventType] = mapped_column(
        enum_column(ProposalEventType, length=30), nullable=False
    )
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    #: Links back to the rule evaluation that caused this, so the timeline explains itself.
    rule_evaluation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    proposal: Mapped[Proposal] = relationship(back_populates="events")

    def __repr__(self) -> str:
        return f"<ProposalEvent {self.type}>"


class Notification(Base, UUIDPrimaryKey):
    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_user_read", "user_id", "read_at"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    type: Mapped[str] = mapped_column(String(60), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str | None] = mapped_column(Text)
    urgency: Mapped[str] = mapped_column(String(16), default="normal", nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(60))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    routed_by_rule_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:
        return f"<Notification {self.type} user={self.user_id}>"
