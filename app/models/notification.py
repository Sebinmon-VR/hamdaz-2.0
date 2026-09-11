"""Something a person needs to be told, and whether they have seen it.

Deliberately generic. The intake pipeline is the first thing to raise one, but
"a tender was assigned to you" and "your leave was approved" are the same shape
of fact, and a notification table per feature is how an application ends up
with four bells in the corner of the screen.

**In-app is the record; Teams is a copy.** A message posted to a Teams channel
cannot be marked read, cannot be listed, and disappears up the channel by the
afternoon. So what happened is stored here and Teams is a second delivery of
it — which also means a Teams webhook that is misconfigured loses a
notification nobody needed twice, rather than the only record of it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Timestamped, UUIDPrimaryKey
from app.models.user import User


class NotificationKind(StrEnum):
    """What sort of thing happened. Decides the icon and how it is grouped."""

    TASK_ASSIGNED = "task_assigned"
    TASK_REOPENED = "task_reopened"
    NEGOTIATION = "negotiation"
    ORDER = "order"
    REPORT_SUBMITTED = "report_submitted"
    MENTION = "mention"
    GENERAL = "general"


class Notification(Base, UUIDPrimaryKey, Timestamped):
    """One thing one person should know about."""

    __tablename__ = "notifications"
    __table_args__ = (
        # The list is always "mine, newest first, unread first", so the index
        # is built for exactly that rather than for a general scan.
        Index("ix_notifications_user_created", "user_id", "created_at"),
        Index("ix_notifications_unread", "user_id", "read_at"),
        # Raising the same notification twice is what happens when a message is
        # retried. Where a caller can name the thing it is about, that name is
        # unique per person and the second attempt is a no-op.
        Index(
            "uq_notification_dedupe",
            "user_id", "source", "source_id",
            unique=True,
            postgresql_where=text("source_id IS NOT NULL"),
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str | None] = mapped_column(Text)
    #: Where to go. A path within the app rather than an absolute URL, so it
    #: survives the frontend moving.
    link: Mapped[str | None] = mapped_column(Text)

    #: Which feature raised it, and the id of the thing it concerns. Together
    #: they make the notification idempotent and let a screen jump to the row.
    source: Mapped[str | None] = mapped_column(String(40))
    source_id: Mapped[str | None] = mapped_column(String(120))
    #: Anything the frontend needs to render it richly — the deadline, the
    #: customer, the task link. Free-shaped on purpose.
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )

    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Whether the Teams copy went out. Kept per notification rather than per
    #: batch, because the useful question is "was this person told" and not
    #: "did the last send succeed".
    sent_to_teams: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )

    user: Mapped[User] = relationship(foreign_keys=[user_id], lazy="joined")

    @property
    def unread(self) -> bool:
        return self.read_at is None

    def __repr__(self) -> str:
        return f"<Notification {self.kind} {self.user_id} {self.title[:30]!r}>"
