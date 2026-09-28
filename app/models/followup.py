"""Asking why a task went past its due date, and what was answered.

A row in the Proposals list has a due date and a status. When the date passes
and the status still says the work is not finished, one of two things is true:
the work is late, or the work is done and nobody moved the status. Either way
a manager wants a sentence from the person holding it, and today they get it
by asking in the corridor.

This module asks instead. A little after the due date and time — the grace is
a setting, twenty minutes by default — the person is mailed a short question
with a link to a form in this app, and told plainly that if they have already
updated the task they can ignore the mail or mark it as a false positive so
nobody chases it again. What they answer goes to the managers and leads of
their team, by mail and by notification, and stays here as a record.

**One ask per deadline.** The pair ``(task_id, due_at)`` is unique: a task is
asked about once for the date it missed, and again only if somebody moves the
date and it is missed again. A row whose task is finished, or whose date is
moved, before anybody answers is resolved quietly rather than left nagging.

**Scoped on purpose, and narrow to begin with.** The settings name one team
whose members are watched, and may narrow further to particular people and to
tasks whose title contains a word — the two filters that make it safe to try
this on one person's test rows before it is turned on for a whole team. Tasks
due before the moment it was switched on are never asked about: an archive of
bids that closed months ago is not something anybody wants two hundred emails
about on the first tick.
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
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Timestamped, UUIDPrimaryKey
from app.models.team import Team
from app.models.user import User


class FollowupStatus(StrEnum):
    #: Asked, not yet answered.
    PENDING = "pending"
    #: The person said why. Their managers have it.
    ANSWERED = "answered"
    #: The person says the task was already dealt with and the ask was wrong —
    #: usually a status they had updated in the meantime, or a due date that
    #: never meant what the list said.
    FALSE_POSITIVE = "false_positive"
    #: Closed by the system, not the person: the task was finished or its due
    #: date moved before anybody answered. ``resolved_note`` says which.
    RESOLVED = "resolved"
    #: Still unanswered at the end of the day, and reported to the CEO as such.
    #: A mark rather than a closure: the person can still answer, and is still
    #: asked to — see ``app.followups.digest``.
    NO_RESPONSE = "no_response"


#: What the person can still act on. Everything else is history.
OPEN_FOLLOWUP_STATUSES: frozenset[str] = frozenset(
    {FollowupStatus.PENDING, FollowupStatus.NO_RESPONSE}
)


class FollowupSettings(Base, Timestamped):
    """What is watched, how long after the due time, and who is asked. One row, id 1."""

    __tablename__ = "followup_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)

    #: The master switch. Off, the loop reads the settings and goes back to
    #: sleep. Turning it on sets ``watch_from`` to that moment.
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    #: Whose tasks are watched: the members of this team, joined to the
    #: Proposals list by email. Null means nobody, not everybody.
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="SET NULL")
    )
    #: Narrower still: only these people, by email. Empty means the whole
    #: team. This is how the feature is tried on one person first.
    only_emails: Mapped[list[str]] = mapped_column(
        ARRAY(String(320)), default=list, server_default=text("'{}'::varchar[]"),
        nullable=False,
    )
    #: Only tasks whose title contains this, case-insensitively. Blank means
    #: every task. "test" while trying it out; blank once it is trusted.
    only_title_contains: Mapped[str] = mapped_column(
        String(120), default="", server_default=text("''"), nullable=False
    )
    #: How long after the due date and time before the question is asked.
    grace_minutes: Mapped[int] = mapped_column(
        Integer, default=20, server_default=text("20"), nullable=False
    )
    #: How often the list is re-read. The grace is the promise; this is how
    #: closely it is kept — an ask lands within one poll of the grace expiring.
    poll_seconds: Mapped[int] = mapped_column(
        Integer, default=120, server_default=text("120"), nullable=False
    )
    #: Tasks due before this moment are never asked about. Set when the
    #: feature is switched on, so an archive of old bids does not become two
    #: hundred emails on the first tick.
    watch_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Whose mailbox the question goes out from. Null falls back to the team's
    #: manager, then its lead, then the person themselves — the mail always
    #: has a sender, and it is somebody the recipient can reply to.
    ask_from_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    #: Mail the managers when a reason is filed, as well as notifying them
    #: in the app. Off leaves the record and the notification.
    notify_managers_by_email: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )

    # ── the end-of-day report ──────────────────────────────────────────
    #: Send the day's reasons as one report at the closing time.
    digest_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    #: The closing time, "HH:MM" on the clock of ``digest_timezone``.
    digest_time: Mapped[str] = mapped_column(
        String(5), default="18:00", server_default=text("'18:00'"), nullable=False
    )
    digest_timezone: Mapped[str] = mapped_column(
        String(64), default="Asia/Kolkata", server_default=text("'Asia/Kolkata'"), nullable=False
    )
    #: Who gets it by address. Sebin while it is tried out.
    digest_recipients: Mapped[list[str]] = mapped_column(
        ARRAY(String(320)), default=list, server_default=text("'{}'::varchar[]"), nullable=False
    )
    #: Also send it to whoever holds the CEO role. Off while it is tried out.
    digest_include_ceo: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    #: "pdf", "xlsx", or both.
    digest_formats: Mapped[list[str]] = mapped_column(
        ARRAY(String(8)), default=lambda: ["pdf", "xlsx"],
        server_default=text("'{pdf,xlsx}'::varchar[]"), nullable=False,
    )
    #: Whose mailbox it is sent from. Null: the first recipient's own.
    digest_sender_email: Mapped[str | None] = mapped_column(String(320))
    #: The day the report last went, in ``digest_timezone`` — so it goes once.
    digest_last_sent_on: Mapped[date | None] = mapped_column(Date)
    digest_last_error: Mapped[str | None] = mapped_column(Text)

    # ── the weekly report ──────────────────────────────────────────────
    #: The same report over the week, sent on one weekday at the closing time,
    #: to the same people. Nobody is marked by it — the daily one does that.
    weekly_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    #: 0 Monday … 6 Sunday. Friday by default, the end of the UAE week.
    weekly_day: Mapped[int] = mapped_column(
        Integer, default=4, server_default=text("4"), nullable=False
    )
    weekly_last_sent_on: Mapped[date | None] = mapped_column(Date)

    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: What went wrong on the last sweep, if anything. A sweep that fails
    #: leaves everything as it was, and this is the only sign it happened.
    last_error: Mapped[str | None] = mapped_column(Text)

    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    team: Mapped[Team | None] = relationship(lazy="joined")
    ask_from: Mapped[User | None] = relationship(foreign_keys=[ask_from_user_id], lazy="joined")

    def __repr__(self) -> str:
        return f"<FollowupSettings enabled={self.enabled} team={self.team_id}>"


class TaskFollowup(Base, UUIDPrimaryKey, Timestamped):
    """One question about one missed deadline, and what came back."""

    __tablename__ = "task_followups"
    __table_args__ = (
        # Asked once per deadline. A moved date is a new deadline and may be
        # asked about again; the same date is not.
        UniqueConstraint("task_id", "due_at", name="uq_task_followup_deadline"),
        # "Mine, newest first" and "this team's, newest first" are the two
        # listings, so both lead the index.
        Index("ix_task_followups_assignee", "assignee_id", "created_at"),
        Index("ix_task_followups_team", "team_id", "created_at"),
        Index("ix_task_followups_status", "status"),
    )

    #: The SharePoint list item, and enough of it to read the row without
    #: going back to the list. Snapshotted at the moment of asking, because
    #: the question was about the task *as it then was*.
    task_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    task_title: Mapped[str] = mapped_column(Text, nullable=False)
    task_url: Mapped[str | None] = mapped_column(Text)
    end_user: Mapped[str | None] = mapped_column(String(300))
    #: The Submission Status the row had when asked — the column that decides
    #: whether a task is done. "Not Submitted" on a bid that went in last week
    #: is a status nobody moved.
    status_at_ask: Mapped[str | None] = mapped_column(String(80))
    #: The due date and time that passed, as SharePoint stated it.
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: When SharePoint last saw the row change, at the moment of asking. The
    #: form compares it with now, so a task edited since the mail went out is
    #: said to have been.
    task_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="SET NULL"), index=True
    )
    assignee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    #: Kept beside the id so the row still reads if the account goes.
    assignee_email: Mapped[str] = mapped_column(String(320), nullable=False)

    #: When the mail went, and what stopped it if it did not. The row exists
    #: either way — the form works from the notification even if the mail
    #: never arrived.
    asked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ask_error: Mapped[str | None] = mapped_column(Text)
    #: Who the ask was sent as.
    asked_from_email: Mapped[str | None] = mapped_column(String(320))

    status: Mapped[str] = mapped_column(
        String(20), default=FollowupStatus.PENDING,
        server_default=text("'pending'"), nullable=False,
    )
    #: What the person said. Null until they do; also null on a false
    #: positive, whose note goes in ``resolved_note``.
    reason: Mapped[str | None] = mapped_column(Text)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: When the managers were told, and what stopped it if they were not.
    forwarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    forward_error: Mapped[str | None] = mapped_column(Text)
    #: Why a row is resolved or marked a false positive, in a sentence.
    resolved_note: Mapped[str | None] = mapped_column(Text)

    assignee: Mapped[User] = relationship(foreign_keys=[assignee_id], lazy="joined")
    team: Mapped[Team | None] = relationship(lazy="joined")

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_FOLLOWUP_STATUSES

    def __repr__(self) -> str:
        return f"<TaskFollowup task={self.task_id} {self.status} {self.assignee_email}>"
