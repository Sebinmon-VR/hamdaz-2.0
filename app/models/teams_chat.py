"""AI employees' own Microsoft 365 accounts, and the Teams chats they answer.

Two tables:

* ``ai_employee_accounts`` — the connection to an AI employee's own account
  (e.g. luna@hamdaz.com): who it is in Entra, the refresh token the app keeps
  (encrypted) so it can act as that account in Teams and Outlook, and how far
  through each chat it has read.
* ``teams_chats`` — one row per person per Teams chat with an AI employee: the
  assistant conversation their messages go into, and a change waiting for
  their yes or no. So a chat in Teams is the same conversation, with the same
  history and audit trail, as one in the app.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamped, UUIDPrimaryKey


class AIEmployeeAccount(Base, Timestamped):
    __tablename__ = "ai_employee_accounts"

    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_employees.id", ondelete="CASCADE"), primary_key=True
    )
    #: The account as Microsoft knows it, from the sign-in.
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    entra_object_id: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(200))
    #: Encrypted with a key derived from the app's secret. Never returned.
    refresh_token_enc: Mapped[str] = mapped_column(Text, nullable=False)
    #: connected, needs_reconnect, disconnected (token wiped, log kept).
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="connected")
    error: Mapped[str | None] = mapped_column(Text)
    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    connected_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    #: Per Teams chat id, the time of the last message already handled.
    watermarks: Mapped[dict | None] = mapped_column(JSONB)
    last_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: What it did with each message it saw, newest last, the latest 100:
    #: ``[{at, from, chat, outcome, detail}]`` — answered, ignored, asked,
    #: error. Shown on the employee's card.
    activity: Mapped[list | None] = mapped_column(JSONB)


class TeamsChat(Base, UUIDPrimaryKey, Timestamped):
    __tablename__ = "teams_chats"
    __table_args__ = (UniqueConstraint("employee_id", "teams_chat_id", "user_id"),)

    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_employees.id", ondelete="CASCADE"), nullable=False
    )
    teams_chat_id: Mapped[str] = mapped_column(String(400), nullable=False)
    #: oneOnOne, group or meeting.
    chat_type: Mapped[str | None] = mapped_column(String(20))
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    assistant_conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assistant_conversations.id", ondelete="SET NULL")
    )
    #: A change the employee asked about and is waiting on a yes or no for.
    pending_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
