"""The status reminder: two days before a task is due, ask its holder where it stands.

Two tables:

* ``status_reminder_settings`` — one row: whether it runs, when, how far
  ahead, its own trial filters, and **whether answers are written to the
  Proposals list** (``write_sharepoint``, shipped off — a super admin turns
  it on).
* ``status_reminders`` — one question about one task's coming deadline:
  what the task said when it was asked, and what the person answered.

Which team is watched, whose mailbox asks and the testing address are the
overdue follow-up's (``FollowupSettings``) — one place to say who the
reminders and the follow-ups are about. This module's own filters can only
narrow that further, for a trial.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Timestamped, UUIDPrimaryKey
from app.models.team import Team
from app.models.user import User


class ReminderStatus(StrEnum):
    #: Asked, not yet answered.
    PENDING = "pending"
    #: The person gave the task's status. ``changes`` says what they changed.
    ANSWERED = "answered"
    #: Closed by the system: the task was completed or submitted, or its due
    #: time passed, before anybody answered. ``closed_note`` says which.
    CLOSED = "closed"


class StatusReminderSettings(Base, Timestamped):
    """The single settings row (``id`` is always 1)."""

    __tablename__ = "status_reminder_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    #: When the day's reminders go, "HH:MM" in the follow-up's time zone.
    ask_time: Mapped[str] = mapped_column(
        String(5), nullable=False, default="10:00", server_default="10:00"
    )
    #: How far ahead of the due time a task is asked about.
    days_before: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2, server_default=text("2")
    )
    #: Whether an answer is written to the task on the Proposals list. Off,
    #: the answer is kept here and shows what it would have written.
    write_sharepoint: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    #: A trial's narrowing of the follow-up's team: only these people (empty
    #: is everybody), and only titles containing this.
    only_emails: Mapped[list[str]] = mapped_column(
        ARRAY(String(320)), nullable=False, default=list, server_default=text("'{}'::varchar[]")
    )
    only_title_contains: Mapped[str] = mapped_column(
        String(200), nullable=False, default="", server_default=""
    )
    #: The day the reminders last went, in that zone — so they go once a day.
    last_run_on: Mapped[date | None] = mapped_column(Date)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


class StatusReminder(Base, UUIDPrimaryKey, Timestamped):
    """One reminder about one task's coming deadline, and the answer."""

    __tablename__ = "status_reminders"
    __table_args__ = (
        # Asked once per deadline. A moved date is a new deadline.
        UniqueConstraint("task_id", "due_at", name="uq_status_reminder_deadline"),
        Index("ix_status_reminders_assignee", "assignee_id", "created_at"),
        Index("ix_status_reminders_status", "status"),
    )

    task_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    task_title: Mapped[str] = mapped_column(Text, nullable=False)
    task_url: Mapped[str | None] = mapped_column(Text)
    end_user: Mapped[str | None] = mapped_column(String(300))
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    #: The task as it was when asked — what the mail showed.
    status_at_ask: Mapped[str | None] = mapped_column(String(80))
    submission_at_ask: Mapped[str | None] = mapped_column(String(80))
    remarks_at_ask: Mapped[str | None] = mapped_column(Text)
    working_notes_at_ask: Mapped[str | None] = mapped_column(Text)

    team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="SET NULL")
    )
    assignee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    assignee_email: Mapped[str] = mapped_column(String(320), nullable=False)

    asked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ask_error: Mapped[str | None] = mapped_column(Text)
    asked_from_email: Mapped[str | None] = mapped_column(String(320))

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ReminderStatus.PENDING,
        server_default=ReminderStatus.PENDING.value,
    )
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: What the answer changed, by SharePoint column name — ``{"Status":
    #: "In Progress", "Remarks": "…"}``. Empty: confirmed as it was.
    changes: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    #: When ``changes`` reached the list. Null with no ``write_error`` while
    #: the write is switched off.
    written_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    write_error: Mapped[str | None] = mapped_column(Text)
    closed_note: Mapped[str | None] = mapped_column(Text)

    assignee: Mapped[User] = relationship(foreign_keys=[assignee_id], lazy="joined")
    team: Mapped[Team | None] = relationship(lazy="joined")

    @property
    def is_open(self) -> bool:
        return self.status == ReminderStatus.PENDING
