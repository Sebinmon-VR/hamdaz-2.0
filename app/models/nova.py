"""Nova, the presales assistant in Teams: who it talks to, and what was said.

Two tables. ``nova_people`` is one row per person who has chatted with Nova:
where their Teams chat with it is (so it can write to them first, later), and
a short summary of what older conversation said. ``nova_messages`` is every
message, theirs and Nova's — the recent ones are Nova's memory of the
conversation, the summary the rest.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamped, UUIDPrimaryKey


class NovaPerson(Base, Timestamped):
    """One person Nova talks to."""

    __tablename__ = "nova_people"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    #: Their 1:1 chat with Nova in Teams, as the bot service names it — what
    #: a message Nova starts itself is addressed to.
    conversation_id: Mapped[str | None] = mapped_column(Text)
    service_url: Mapped[str | None] = mapped_column(Text)
    #: What the conversation before the recent messages said, in a few lines.
    summary: Mapped[str | None] = mapped_column(Text)
    #: Messages up to here are in ``summary``.
    summarized_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NovaMessage(Base, UUIDPrimaryKey, Timestamped):
    """One message in a person's chat with Nova."""

    __tablename__ = "nova_messages"
    __table_args__ = (Index("ix_nova_messages_user_created", "user_id", "created_at"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    #: "person" or "nova".
    author: Mapped[str] = mapped_column(String(10), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    #: On Nova's messages: who wrote the words ("groq:openai/gpt-oss-120b", or
    #: "rules" when no model was needed), and how long the reply took.
    written_by: Mapped[str | None] = mapped_column(String(120))
    took_ms: Mapped[int | None] = mapped_column(Integer)
