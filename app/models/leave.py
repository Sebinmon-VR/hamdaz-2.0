"""Leave requests, and the rules HR sets for deciding them.

Everyone can raise a request; only HR decides one. The interesting part is that
most decisions are made by the system: a request that fits within the concurrent
limit is approved on submission, one that does not is rejected with the reason.
HR overrides either way, which is what "emergency" means here.

Dates are stored as plain dates, not timestamps. Leave is granted in whole days,
and a timezone on "the 3rd of June" only creates ways for it to become the 2nd.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Timestamped, UUIDPrimaryKey
from app.models.user import User


class LeaveStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class LeaveType(StrEnum):
    ANNUAL = "annual"
    SICK = "sick"
    EMERGENCY = "emergency"
    UNPAID = "unpaid"


class DecisionBy(StrEnum):
    """Who settled it. Worth recording: an auto-rejection reads very differently
    from a person saying no, and HR needs to tell them apart."""

    SYSTEM = "system"
    HR = "hr"
    #: The requester withdrew it themselves.
    REQUESTER = "requester"


class LeaveRequest(Base, UUIDPrimaryKey, Timestamped):
    __tablename__ = "leave_requests"
    __table_args__ = (
        CheckConstraint("end_date >= start_date", name="ck_leave_dates_ordered"),
        # Every overlap query is "who else is off between these dates".
        Index("ix_leave_range", "start_date", "end_date", "status"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    leave_type: Mapped[LeaveType] = mapped_column(String(20), nullable=False)

    #: Inclusive on both ends: a one-day leave has start == end.
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)

    status: Mapped[LeaveStatus] = mapped_column(
        String(20), default=LeaveStatus.PENDING, nullable=False, index=True
    )
    decided_by: Mapped[DecisionBy | None] = mapped_column(String(20))
    decided_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Always populated on a rejection — a refusal without a reason is useless.
    decision_note: Mapped[str | None] = mapped_column(Text)

    #: Approved by HR despite the concurrent limit. Kept as a flag rather than
    #: inferred, so "how often do we override the rule" is answerable.
    emergency_override: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )

    #: How many people were already off on the busiest day of this range when it
    #: was decided. Frozen at decision time so the reason stays true later.
    conflicting_count: Mapped[int | None] = mapped_column(Integer)

    #: Whether the notification email actually went out.
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notify_error: Mapped[str | None] = mapped_column(Text)

    user: Mapped[User] = relationship(foreign_keys=[user_id], lazy="joined")

    @property
    def days(self) -> int:
        return (self.end_date - self.start_date).days + 1

    @property
    def is_open(self) -> bool:
        return self.status == LeaveStatus.PENDING

    def __repr__(self) -> str:
        return f"<LeaveRequest {self.user_id} {self.start_date}..{self.end_date} {self.status}>"


class LeaveSettings(Base, Timestamped):
    """HR's rules. One row; ``id`` is fixed at 1.

    A single row rather than a key/value table: these settings are read together
    on every submission, and a typo in a key should be a schema error rather than
    a silently ignored rule.
    """

    __tablename__ = "leave_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)

    #: How many people may be on approved leave on the same day.
    max_concurrent: Mapped[int] = mapped_column(
        Integer, default=2, server_default=text("2"), nullable=False
    )
    #: When false, everything lands as pending and waits for a human.
    auto_decide: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    #: "organisation" counts everyone; "team" counts only the requester's teams,
    #: so a busy day in one team does not block another.
    limit_scope: Mapped[str] = mapped_column(
        String(20), default="organisation", server_default=text("'organisation'"), nullable=False
    )
    #: The team whose members act as HR.
    hr_team_slug: Mapped[str] = mapped_column(
        String(64), default="hr", server_default=text("'hr'"), nullable=False
    )
    #: Send the request to HR by email, from the requester's own mailbox.
    #: Off by default so a development build cannot mail real colleagues.
    notify_hr_by_email: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    #: Requests further ahead than this are refused as probably a typo.
    max_days_per_request: Mapped[int] = mapped_column(
        Integer, default=30, server_default=text("30"), nullable=False
    )

    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    def __repr__(self) -> str:
        return f"<LeaveSettings max_concurrent={self.max_concurrent}>"
