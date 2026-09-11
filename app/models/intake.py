"""Work that arrives by email, and what was decided about it.

A tender lands in somebody's inbox as a message from the CEO. Today a person
reads it, works out whether it is something already in the Proposals list or
something new, finds who should take it, and types it in. This module is that
person's judgement written down: watch a mailbox, decide what the mail is
about, look for it in the list, and either raise it or tell whoever holds it.

**Every message gets a row, including the ones that are ignored.** A pipeline
that only records what it acted on cannot answer the question people actually
ask of it, which is "why did nothing happen when I sent that". The row carries
what the model decided, what it matched against and why, and what was done —
so a wrong answer can be seen to be wrong instead of merely being absent.

**Writing to SharePoint is a switch, and it ships off.** The Proposals list is
live and the team works in it, so until somebody deliberately turns
``create_in_sharepoint`` on, a message that would raise a task records exactly
what it would have posted and posts nothing. That is not a lesser mode for
testing; it is how this runs until its judgement has been watched for a while.
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
    Integer,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Timestamped, UUIDPrimaryKey
from app.models.user import User


class MailCategory(StrEnum):
    """What a message is about. The branch the pipeline takes."""

    #: A tender or bid invitation — the thing that becomes a proposal task.
    TENDER = "tender"
    #: A request to quote, which is the same shape of work under another name.
    PROPOSAL = "proposal"
    #: A customer coming back on a price. Never raises a task; the work already
    #: exists and somebody holds it.
    NEGOTIATION = "negotiation"
    #: A purchase order against something already quoted.
    ORDER = "order"
    #: Circulars, acknowledgements, thanks. Recorded and left alone.
    GENERAL = "general"
    #: The model could not say. Recorded rather than guessed at, because a bad
    #: guess here creates a task assigned to a real person.
    UNKNOWN = "unknown"


#: The categories that can bring new work into the list. Negotiation and order
#: concern something that already exists, so they never create — they find the
#: task and tell whoever holds it.
CREATING_CATEGORIES: frozenset[str] = frozenset({MailCategory.TENDER, MailCategory.PROPOSAL})


class IntakeStatus(StrEnum):
    #: Seen, not yet looked at.
    RECEIVED = "received"
    #: Read by the model: category and details are on the row.
    CLASSIFIED = "classified"
    #: Something was done — a task raised, or somebody told.
    ACTIONED = "actioned"
    #: Deliberately nothing: the wrong sender, a general circular, a category
    #: the settings say to leave alone.
    IGNORED = "ignored"
    #: Would have acted, but writing to SharePoint is switched off. What it
    #: would have posted is on the row.
    SIMULATED = "simulated"
    #: Something went wrong. The message is kept so it can be tried again.
    FAILED = "failed"


class IntakeAction(StrEnum):
    NONE = "none"
    CREATED_TASK = "created_task"
    #: The task exists and has been reopened; whoever holds it was told.
    REOPENED_NOTICE = "reopened_notice"
    NEGOTIATION_NOTICE = "negotiation_notice"
    #: The matched task's ``Negotiation`` column was set, which is what a flow
    #: watching the list triggers on. A stronger outcome than the notice above:
    #: something outside this system now knows.
    MARKED_NEGOTIATION = "marked_negotiation"
    ORDER_NOTICE = "order_notice"
    #: Matched something, and the message added nothing worth telling anybody.
    DUPLICATE = "duplicate"


class IntakeSettings(Base, Timestamped):
    """What the pipeline watches and what it is allowed to do. One row, id 1."""

    __tablename__ = "intake_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)

    #: The master switch. Off means the loops do not run at all.
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    #: Whose inbox is watched. A super admin can point this at somebody else —
    #: the person who actually receives the tenders is not always the person
    #: administering the system.
    mailbox: Mapped[str] = mapped_column(
        String(320), default="", server_default=text("''"), nullable=False
    )
    #: Only mail from these addresses is looked at. Empty means *nobody*, not
    #: everybody: an intake that reads every message in a mailbox and creates
    #: tasks from it is not something anybody should get by leaving a box blank.
    allowed_senders: Mapped[list[str]] = mapped_column(
        ARRAY(String(320)), default=list, server_default=text("'{}'::varchar[]"),
        nullable=False,
    )
    #: Whole domains, for a customer whose staff all write from one. Same rule:
    #: empty is not a wildcard.
    allowed_domains: Mapped[list[str]] = mapped_column(
        ARRAY(String(200)), default=list, server_default=text("'{}'::varchar[]"),
        nullable=False,
    )

    #: **The live-write switch, and it ships off.** On, a new tender becomes a
    #: real row in the Proposals list assigned to a real person. Off, the row
    #: that would have been created is recorded and nothing is posted.
    create_in_sharepoint: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    #: The second write, and its own switch. When a negotiation email matches a
    #: task, set that task's ``Negotiation`` column — which is what a Power
    #: Automate flow watching the list can trigger on.
    #:
    #: Separate from ``create_in_sharepoint`` deliberately: marking a column on
    #: a row that already exists is a much smaller act than creating a row and
    #: assigning it to somebody, and an administrator may reasonably want one
    #: without the other.
    update_negotiation: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    #: What to write there. ``Negotiation`` is a choice column offering Yes and
    #: No; this is a setting rather than a constant because a list's choices are
    #: the list's business and can be changed without touching this code.
    negotiation_value: Mapped[str] = mapped_column(
        String(60), default="Yes", server_default=text("'Yes'"), nullable=False
    )
    #: Which team's ranking decides who a new task goes to. Presales, normally.
    assign_team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="SET NULL")
    )

    #: How sure the model must be that a message matches an existing task
    #: before the pipeline treats it as the same thing. Below this it is
    #: treated as unmatched, which for a tender means raising a new task — so
    #: this is the dial between duplicates and missed links.
    match_threshold: Mapped[float] = mapped_column(
        Numeric(3, 2), default=0.7, server_default=text("0.70"), nullable=False
    )
    #: How sure it must be about the category before acting on it at all.
    classify_threshold: Mapped[float] = mapped_column(
        Numeric(3, 2), default=0.6, server_default=text("0.60"), nullable=False
    )

    #: Where Teams messages go. An incoming webhook URL, created in the channel
    #: itself — no Graph permission, and it works the day it is pasted in.
    teams_webhook_url: Mapped[str | None] = mapped_column(Text)
    notify_in_app: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    notify_teams: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )

    #: How often the mailbox is polled. The webhook, when it is set up, makes
    #: this the safety net rather than the mechanism — Microsoft's own advice
    #: is not to depend on notifications alone.
    poll_seconds: Mapped[int] = mapped_column(
        Integer, default=60, server_default=text("60"), nullable=False
    )
    #: Graph's delta cursor for the watched folder. Null means the next poll
    #: starts from now rather than replaying the whole inbox — which is what
    #: should happen when a mailbox is first configured.
    delta_link: Mapped[str | None] = mapped_column(Text)
    #: Nothing older than this is ever processed, however it arrives. Stops a
    #: newly pointed mailbox raising tasks from a year of history.
    watch_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: The Graph change-notification subscription, when one is active.
    subscription_id: Mapped[str | None] = mapped_column(String(120))
    subscription_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Sent back to us on every notification, and checked. A webhook endpoint
    #: that acts on whatever posts to it is an open door.
    subscription_secret: Mapped[str | None] = mapped_column(String(120))

    last_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    def __repr__(self) -> str:
        return f"<IntakeSettings {self.mailbox} enabled={self.enabled}>"


class IntakeMessage(Base, UUIDPrimaryKey, Timestamped):
    """One email, what was made of it, and what was done.

    Kept whatever the outcome. The ignored ones are the most useful rows in the
    table when somebody asks why their message did nothing.
    """

    __tablename__ = "intake_messages"
    __table_args__ = (
        # Graph will deliver the same message twice — a notification and a poll
        # racing, or a retried webhook. The unique id is what makes that a
        # no-op instead of two tasks.
        Index("uq_intake_message_graph_id", "graph_message_id", unique=True),
        Index("ix_intake_messages_received", "received_at"),
        Index("ix_intake_messages_status", "status"),
        Index("ix_intake_messages_category", "category"),
    )

    graph_message_id: Mapped[str] = mapped_column(String(512), nullable=False)
    #: Graph's conversation id, so a reply can be tied to the thread that
    #: raised the task rather than treated as a new tender.
    conversation_id: Mapped[str | None] = mapped_column(String(512), index=True)
    internet_message_id: Mapped[str | None] = mapped_column(String(998))

    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sender_email: Mapped[str | None] = mapped_column(String(320), index=True)
    sender_name: Mapped[str | None] = mapped_column(String(200))
    subject: Mapped[str | None] = mapped_column(Text)
    #: The body, trimmed. Kept because the classification is only auditable
    #: against what the model was actually shown.
    body: Mapped[str | None] = mapped_column(Text)
    has_attachments: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    web_link: Mapped[str | None] = mapped_column(Text)

    status: Mapped[str] = mapped_column(
        String(16), default=IntakeStatus.RECEIVED,
        server_default=text("'received'"), nullable=False,
    )

    # ── what the model made of it ──────────────────────────────────────
    category: Mapped[str | None] = mapped_column(String(24))
    #: Whether the mail says this is coming back rather than arriving new.
    #: Decided from the words, then checked against whether it matched.
    is_reopened: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    confidence: Mapped[float | None] = mapped_column(Numeric(3, 2))
    #: Title, customer, references, dates — whatever was found. Free-shaped
    #: because what a tender email carries is not what a negotiation does.
    extracted: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    #: In the model's own words. The first thing anybody reads when the
    #: classification looks wrong.
    reasoning: Mapped[str | None] = mapped_column(Text)

    # ── what it was matched against ────────────────────────────────────
    matched_item_id: Mapped[str | None] = mapped_column(String(64), index=True)
    match_confidence: Mapped[float | None] = mapped_column(Numeric(3, 2))
    match_reason: Mapped[str | None] = mapped_column(Text)
    #: The shortlist it chose from, with each one's score. Kept so a wrong
    #: match can be seen to have been a close call rather than a wild guess.
    candidates: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )

    # ── what was done ──────────────────────────────────────────────────
    action: Mapped[str] = mapped_column(
        String(24), default=IntakeAction.NONE, server_default=text("'none'"), nullable=False
    )
    #: Who it was given to, when it created something.
    assigned_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    assigned_reason: Mapped[str | None] = mapped_column(String(200))
    #: The SharePoint item that was created. Null while writing is switched off.
    created_item_id: Mapped[str | None] = mapped_column(String(64))
    #: Exactly what would be posted to SharePoint, field for field. This is the
    #: whole of the switched-off mode: the decision is complete and inspectable,
    #: and only the last step is missing.
    would_create: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    #: The same for a change to an existing row — setting ``Negotiation`` on the
    #: matched task. Filled whether or not it was sent, and left in place after
    #: it was, so the row records what was actually written rather than only
    #: that something happened.
    would_update: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    notified_user_ids: Mapped[list[str]] = mapped_column(
        ARRAY(String(64)), default=list, server_default=text("'{}'::varchar[]"),
        nullable=False,
    )
    notified_teams: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )

    error: Mapped[str | None] = mapped_column(Text)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: What the model cost, so the bill is attributable to the feature that
    #: caused it rather than appearing as a lump on somebody's OpenAI account.
    cost_usd: Mapped[float | None] = mapped_column(Numeric(10, 6))

    assigned_user: Mapped[User | None] = relationship(
        foreign_keys=[assigned_user_id], lazy="joined"
    )

    @property
    def is_done(self) -> bool:
        return self.status in (
            IntakeStatus.ACTIONED, IntakeStatus.IGNORED, IntakeStatus.SIMULATED
        )

    def __repr__(self) -> str:
        return f"<IntakeMessage {self.subject!r} {self.category} {self.status}>"
