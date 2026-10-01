"""Task calendar: each open task's BCD as an event in its holder's Outlook calendar.

Two tables:

* ``task_calendar_settings`` — one row: whether it runs, how long before the
  BCD Outlook reminds, and a trial's narrowing (people, title word).
* ``task_calendar_events`` — the event this put in somebody's calendar for a
  task: whose calendar, its Outlook id, and the BCD it was set to — so a moved
  BCD moves it, a reassignment moves it to the new holder, and a submitted
  task takes it out.

A task whose BCD is still the assignment-time placeholder gets no event until
the BCD is real (see ``app.bcd``).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Timestamped, UUIDPrimaryKey
from app.models.user import User


class TaskCalendarSettings(Base, Timestamped):
    """The single settings row (``id`` is always 1)."""

    __tablename__ = "task_calendar_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    #: Outlook's reminder, this many minutes before the BCD. Two days.
    reminder_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2880, server_default=text("2880")
    )
    #: A trial's narrowing of the follow-up's team: only these people (empty is
    #: everybody), and only titles containing this.
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


class TaskCalendarEvent(Base, UUIDPrimaryKey, Timestamped):
    """One task's event in one person's calendar."""

    __tablename__ = "task_calendar_events"

    #: One event per task: a reassigned task's event moves calendars.
    task_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    task_title: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    #: Whose calendar it is in, as Graph addresses it.
    owner: Mapped[str] = mapped_column(String(320), nullable=False)
    event_id: Mapped[str] = mapped_column(Text, nullable=False)
    #: The BCD the event is set to, and the reminder it carries.
    bcd_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reminder_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)

    user: Mapped[User] = relationship(lazy="joined")
