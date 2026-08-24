"""Leave management (§9 Phase 7).

The legacy system spread this across 14 routes and a Cosmos container, with the concurrency
rules and approval chain baked into Python. Here the policy lives in the rules engine
(``leave.eligibility``), and handing over ongoing proposals reuses the assignment engine.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Date,
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
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamped, UUIDPrimaryKey, enum_column


class LeaveStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class LeaveType(StrEnum):
    ANNUAL = "annual"
    SICK = "sick"
    UNPAID = "unpaid"
    EMERGENCY = "emergency"
    PARENTAL = "parental"


class LeaveRequest(Base, UUIDPrimaryKey, Timestamped):
    __tablename__ = "leave_requests"
    __table_args__ = (
        Index("ix_leave_requests_user_status", "user_id", "status"),
        Index("ix_leave_requests_team_dates", "team_id", "start_date", "end_date"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="SET NULL"), index=True
    )
    type: Mapped[LeaveType] = mapped_column(enum_column(LeaveType, length=20), nullable=False)
    #: Stored as timestamps so overlap checks are a simple range comparison.
    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    days: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)

    status: Mapped[LeaveStatus] = mapped_column(
        enum_column(LeaveStatus, length=16),
        default=LeaveStatus.PENDING,
        nullable=False,
        index=True,
    )
    approver_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    remarks: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: Who inherits the requester's open proposals while they are away.
    handoff_to: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    handoff_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Links to the ``leave.eligibility`` evaluation that decided the routing.
    routed_by_rule_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    def overlaps(self, start: datetime, end: datetime) -> bool:
        return self.start_date <= end and self.end_date >= start

    def __repr__(self) -> str:
        return f"<LeaveRequest {self.user_id} {self.status}>"


class Holiday(Base, UUIDPrimaryKey, Timestamped):
    __tablename__ = "holidays"
    __table_args__ = (
        UniqueConstraint("team_id", "title", "start_date", name="uq_holidays_team_title_start"),
    )

    #: NULL means org-wide.
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE")
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    end_date: Mapped[date | None] = mapped_column(Date)
    #: ``holiday`` or ``blackout`` — a blackout period refuses leave rather than granting it.
    type: Mapped[str] = mapped_column(String(20), default="holiday", nullable=False)

    def __repr__(self) -> str:
        return f"<Holiday {self.title}>"


class LeaveSetting(Base, Timestamped):
    """Per-team leave configuration, edited in the admin panel."""

    __tablename__ = "leave_settings"
    __table_args__ = (UniqueConstraint("team_id", "key", name="uq_leave_settings_team_key"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE")
    )
    key: Mapped[str] = mapped_column(String(60), nullable=False)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    def __repr__(self) -> str:
        return f"<LeaveSetting {self.key}>"


#: Defaults applied when a team has configured nothing.
DEFAULT_LEAVE_SETTINGS: dict[str, Any] = {
    "annual_allowance_days": {"value": 30},
    "max_concurrent_leave": {"value": 2},
    "min_notice_days": {"value": 7},
    "require_handoff_above_days": {"value": 3},
}

LEAVE_ALLOWANCE_KEY = "annual_allowance_days"
MAX_CONCURRENT_KEY = "max_concurrent_leave"


class LeaveBalance(Base, Timestamped):
    """Cached remaining allowance, recomputed rather than trusted."""

    __tablename__ = "leave_balances"
    __table_args__ = (UniqueConstraint("user_id", "year", "type", name="uq_leave_balances"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[LeaveType] = mapped_column(enum_column(LeaveType, length=20), nullable=False)
    allowance_days: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)
    taken_days: Mapped[float] = mapped_column(Numeric(5, 2), default=0, nullable=False)

    @property
    def remaining_days(self) -> float:
        return float(self.allowance_days) - float(self.taken_days)
