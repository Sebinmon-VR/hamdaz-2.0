"""BCD confirmation: a task whose BCD is the assignment time, held until a person confirms it.

The flow that creates Proposals rows has to fill the BCD (the column is
mandatory there), and it fills it with the moment of assignment — the real
closing date is in Ariba, read by a person. So a new row's BCD is, at first, a
placeholder. Taken at its word it makes a task due the day it arrives, and the
overdue follow-up asks why it is late within hours of being assigned.

Two tables:

* ``bcd_check_settings`` — one row: whether it runs, the working hours and
  days, how long before it goes to the managers, a trial's narrowing.
* ``bcd_checks`` — one task with a placeholder BCD: when it was asked about,
  escalated, and how it ended.

Nothing here writes to SharePoint. The BCD is corrected by a person in the
list itself; the check sees the change on its next read and closes.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Timestamped, UUIDPrimaryKey
from app.models.team import Team
from app.models.user import User


class BcdCheckStatus(StrEnum):
    #: The BCD is still the placeholder, and nobody has said it is right.
    PENDING = "pending"
    #: Somebody changed the BCD on the list: it is a real date now.
    CORRECTED = "corrected"
    #: Somebody said the date as it stands is right.
    CONFIRMED = "confirmed"
    #: Closed by the system: the task was submitted, completed or removed.
    CLOSED = "closed"


class BcdCheckSettings(Base, Timestamped):
    """The single settings row (``id`` is always 1)."""

    __tablename__ = "bcd_check_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    #: Off: nobody is asked, and no other module holds a task for its BCD.
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    #: Working hours, "HH:MM", in ``timezone``. Questions wait for them.
    work_start: Mapped[str] = mapped_column(
        String(5), nullable=False, default="10:00", server_default="10:00"
    )
    work_end: Mapped[str] = mapped_column(
        String(5), nullable=False, default="18:00", server_default="18:00"
    )
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="Asia/Kolkata", server_default="Asia/Kolkata"
    )
    #: Working days, Monday = 0. Monday to Saturday.
    work_days: Mapped[list[int]] = mapped_column(
        ARRAY(Integer), nullable=False, default=lambda: [0, 1, 2, 3, 4, 5],
        server_default=text("'{0,1,2,3,4,5}'::integer[]"),
    )
    #: Working minutes unanswered before it goes to the managers, the
    #: approvers and the super admins.
    escalate_after_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=120, server_default=text("120")
    )
    #: Only tasks created from here are asked about — switching it on does not
    #: send the backlog. Set when it is switched on.
    watch_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: A trial's narrowing of the follow-up's team: only these people (empty is
    #: everybody) and only titles containing this.
    only_emails: Mapped[list[str]] = mapped_column(
        ARRAY(String(320)), nullable=False, default=list, server_default=text("'{}'::varchar[]")
    )
    only_title_contains: Mapped[str] = mapped_column(
        String(200), nullable=False, default="", server_default=""
    )
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


class BcdCheck(Base, UUIDPrimaryKey, Timestamped):
    """One task whose BCD is the placeholder, and what became of it."""

    __tablename__ = "bcd_checks"
    __table_args__ = (Index("ix_bcd_checks_status", "status"),)

    #: One per task: a task's placeholder is asked about once.
    task_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    task_title: Mapped[str] = mapped_column(Text, nullable=False)
    task_url: Mapped[str | None] = mapped_column(Text)
    #: When the row was created on the list, and the BCD it carried then.
    task_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    placeholder_bcd: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="SET NULL")
    )
    assignee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    assignee_email: Mapped[str] = mapped_column(String(320), nullable=False)

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=BcdCheckStatus.PENDING,
        server_default=BcdCheckStatus.PENDING.value,
    )
    #: The question to the person, team lead copied — sent in working hours.
    asked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ask_error: Mapped[str | None] = mapped_column(Text)
    #: Unanswered for the working time set: to the managers, approvers and
    #: super admins, team lead copied.
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    escalate_error: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    #: How it ended, in words: the new BCD, "confirmed as it is", why closed.
    resolved_note: Mapped[str | None] = mapped_column(Text)

    assignee: Mapped[User] = relationship(foreign_keys=[assignee_id], lazy="joined")
    resolved_by: Mapped[User | None] = relationship(foreign_keys=[resolved_by_id], lazy="joined")
    team: Mapped[Team | None] = relationship(lazy="joined")

    @property
    def is_open(self) -> bool:
        return self.status == BcdCheckStatus.PENDING
