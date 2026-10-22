"""Mail watch: the CEO's mails to presales, read as they arrive.

Two tables:

* ``mailwatch_settings`` — one row: which mailbox is watched, whose mail in it
  counts, the switch, and Graph's subscription and delta bookmark for it.
* ``watched_mails`` — one mail from a watched sender, as it arrived.

Read only. Nothing here marks a mail read, moves it, or writes to SharePoint.
The mailbox is a setting rather than an environment value because it is
expected to change — from one person's inbox to a shared one — without a
redeploy.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamped, UUIDPrimaryKey


class MailWatchSettings(Base, Timestamped):
    """The single settings row (``id`` is always 1)."""

    __tablename__ = "mailwatch_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    #: Off: no subscription is kept and a notification is ignored.
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    #: The mailbox read. Changing it starts that mailbox afresh from the moment
    #: of the change — its history is not read.
    mailbox: Mapped[str] = mapped_column(String(320), nullable=False, default="", server_default="")
    #: Whose mail counts. Empty means whoever holds the CEO role.
    senders: Mapped[list[str]] = mapped_column(
        ARRAY(String(320)), nullable=False, default=list, server_default=text("'{}'::varchar[]")
    )
    #: Whether a model may settle what the rules cannot. Used from step 2.
    use_model: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    #: Mail received before this is never read. Set when the mailbox changes.
    watch_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Graph's bookmark: "what changed since here".
    delta_link: Mapped[str | None] = mapped_column(Text)

    subscription_id: Mapped[str | None] = mapped_column(String(100))
    #: Checked on every notification; a webhook that trusts any caller is open.
    subscription_secret: Mapped[str | None] = mapped_column(String(100))
    subscription_mailbox: Mapped[str | None] = mapped_column(String(320))
    subscription_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    last_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


class WatchedMail(Base, UUIDPrimaryKey, Timestamped):
    """One mail from a watched sender."""

    __tablename__ = "watched_mails"
    __table_args__ = (
        # Graph can report the same mail twice (a notification and a catch-up);
        # the second is a no-op.
        UniqueConstraint("mailbox", "graph_message_id", name="uq_watched_mails_message"),
        Index("ix_watched_mails_received_at", "received_at"),
        Index("ix_watched_mails_conversation_id", "conversation_id"),
        Index("ix_watched_mails_category", "category"),
        Index("ix_watched_mails_ariba_doc", "ariba_doc"),
        Index("ix_watched_mails_matched_item_id", "matched_item_id"),
    )

    mailbox: Mapped[str] = mapped_column(String(320), nullable=False)
    graph_message_id: Mapped[str] = mapped_column(String(300), nullable=False)
    #: The Outlook thread. A reply in it is the same piece of work.
    conversation_id: Mapped[str | None] = mapped_column(String(300))
    #: The mail's own id, the same in every mailbox it reached.
    internet_message_id: Mapped[str | None] = mapped_column(String(500))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sender_email: Mapped[str | None] = mapped_column(String(320))
    sender_name: Mapped[str | None] = mapped_column(String(200))
    subject: Mapped[str | None] = mapped_column(Text)
    #: Plain text, trimmed.
    body: Mapped[str | None] = mapped_column(Text)
    has_attachments: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    attachment_names: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list, server_default=text("'{}'::text[]")
    )
    web_link: Mapped[str | None] = mapped_column(Text)

    # ── step 2: what it is (app/mailwatch/classify.py) ──
    #: new_tender, negotiation, general or confidential. Empty until read.
    category: Mapped[str | None] = mapped_column(String(20))
    #: Finer: invitation, reminder, closed, buyer_message, order, credential…
    kind: Mapped[str | None] = mapped_column(String(30))
    #: eoi, rfi, rfq, rfp, rft, pre_tender or negotiation, when it says.
    stage: Mapped[str | None] = mapped_column(String(20))
    #: rules, model, or none (neither could place it).
    decided_by: Mapped[str | None] = mapped_column(String(10))
    reason: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    title: Mapped[str | None] = mapped_column(Text)
    customer: Mapped[str | None] = mapped_column(String(200))
    #: Tender, RFQ, PR, PO and portal numbers found in it.
    reference_numbers: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list, server_default=text("'{}'::text[]")
    )
    #: Ariba's document number — the same on every notice about one event.
    ariba_doc: Mapped[str | None] = mapped_column(String(30))
    closing_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Who the forwarded mail was originally from.
    original_sender: Mapped[str | None] = mapped_column(String(320))
    model_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 5), nullable=False, default=Decimal("0"), server_default=text("0")
    )
    classified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    classify_error: Mapped[str | None] = mapped_column(Text)

    # ── step 3: is it already on the Proposals list (app/mailwatch/matching.py) ──
    #: new, existing, stage_change, duplicate_mail, not_found or unsure.
    #: Empty for a category that is not matched (general, confidential).
    verdict: Mapped[str | None] = mapped_column(String(20))
    #: The Proposals list item it was matched to.
    matched_item_id: Mapped[str | None] = mapped_column(String(64))
    #: earlier_mail, reference, title or model.
    match_method: Mapped[str | None] = mapped_column(String(20))
    match_score: Mapped[float | None] = mapped_column(Float)
    match_reason: Mapped[str | None] = mapped_column(Text)
    #: The best few rows considered, with their scores — for checking a match.
    match_candidates: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    #: The matched task as SharePoint had it when matched: assignee, status,
    #: stage, quote number, BCD, negotiation, remarks, link.
    task: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    #: The first mail that brought the same tender, when this one repeats it.
    duplicate_of_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("watched_mails.id", ondelete="SET NULL")
    )
    matched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    match_error: Mapped[str | None] = mapped_column(Text)
