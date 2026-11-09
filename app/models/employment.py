"""Who is leaving, and what HR did about it.

A resignation is kept in its own table rather than as columns on ``users``.
Every query in the app loads users; columns there would break all of it until
the migration ran, and would put HR facts into a table that is about sign-in.
One row per person who is leaving or has left. Withdrawing a resignation, or
taking somebody back after they left, deletes the row — the event log keeps the
history.

Status is worked out from the dates, never stored:

* no row — employed;
* a row, today on or before the last working day — serving notice;
* a row, today after it — left.

That is what makes the exclusion from assignment automatic: the ranking reads
the row, so there is no flag to remember to set and no job that has to run on
the day.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import Date, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Timestamped, UUIDPrimaryKey
from app.models.user import User


class ExitKind(StrEnum):
    RESIGNED = "resigned"
    TERMINATED = "terminated"
    CONTRACT_ENDED = "contract_ended"
    OTHER = "other"


class EmployeeExit(Base, UUIDPrimaryKey, Timestamped):
    __tablename__ = "employee_exits"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    kind: Mapped[str] = mapped_column(
        String(20),
        default=ExitKind.RESIGNED,
        server_default=ExitKind.RESIGNED.value,
        nullable=False,
    )
    #: The day they handed in their notice. Unset for a dismissal with no notice.
    resigned_on: Mapped[date | None] = mapped_column(Date)
    #: Inclusive: they can still sign in on this day, and not after it.
    last_working_day: Mapped[date] = mapped_column(Date, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    recorded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    #: When this module switched their sign-in off. Set once, so somebody an
    #: admin deliberately lets back in (to collect a payslip, say) is not
    #: switched off again by the next sweep.
    access_ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: The payroll record whose leaving date this module filled in, and the
    #: date it wrote — so a withdrawn resignation can undo exactly that and
    #: nothing an accountant typed by hand.
    payroll_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payroll_employees.id", ondelete="SET NULL")
    )
    payroll_left_on_set: Mapped[date | None] = mapped_column(Date)

    user: Mapped[User] = relationship(foreign_keys=[user_id], lazy="joined")
    recorded_by: Mapped[User | None] = relationship(foreign_keys=[recorded_by_id], lazy="joined")

    def status(self, today: date) -> str:
        return "notice" if today <= self.last_working_day else "left"

    def __repr__(self) -> str:
        return f"<EmployeeExit user={self.user_id} last={self.last_working_day}>"


class EmploymentEvent(Base, UUIDPrimaryKey, Timestamped):
    """Every change to someone's employment, and who made it.

    Apart from the exit row on purpose: withdrawing a resignation deletes that
    row, and the record that it was ever handed in must survive it.
    """

    __tablename__ = "employment_events"
    __table_args__ = (Index("ix_employment_events_user_created", "user_id", "created_at"),)

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )

    actor: Mapped[User | None] = relationship(foreign_keys=[actor_id], lazy="joined")
