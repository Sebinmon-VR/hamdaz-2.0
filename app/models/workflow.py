"""Workflows: a team's process written down as steps, and each time it is run.

A *workflow* is a definition — an ordered list of steps, each one a block from
a small catalogue: call one of the app's own routes, ask the person something,
read documents, send a mail, wait for a reply, compare what came back. A
super admin arranges the blocks; nothing here is code a deploy has to change.

A *run* is one execution of a workflow for one subject — for presales, one
Proposals task. It walks the steps in order and stops wherever a step has to
wait: on the person (a question, a "verified" click), or on the world (a
supplier's reply, an approval). Everything it learns along the way lives in
``context``, a JSON object each step reads from and writes to, so the whole
state of a run is one row and a crashed process resumes exactly where it was.

**Nothing here writes to SharePoint, Zoho or a mailbox on its own.** The steps
that would are held behind three switches on ``WorkflowSettings`` that ship
off, and behind the person's own "go ahead" inside the run. The rule the
module is built around is that an automated flow may *prepare* anything and
*do* only what somebody has switched on and somebody has approved.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Timestamped, UUIDPrimaryKey
from app.models.team import Team
from app.models.user import User


class WorkflowTrigger(StrEnum):
    #: Somebody presses "start" on a task, or the assistant does for them.
    MANUAL = "manual"
    #: A run starts by itself when an open task is assigned to somebody on
    #: the workflow's team. Off by default on the shipped flow: an automatic
    #: start means automatic mails once the switches are on.
    TASK_ASSIGNED = "task_assigned"


class RunStatus(StrEnum):
    RUNNING = "running"
    #: Stopped on a question or a review the person has to answer.
    WAITING_USER = "waiting_user"
    #: Stopped on something outside: a supplier's reply, an approval.
    WAITING_EVENT = "waiting_event"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


OPEN_RUN_STATUSES: tuple[RunStatus, ...] = (
    RunStatus.RUNNING,
    RunStatus.WAITING_USER,
    RunStatus.WAITING_EVENT,
)


class RunEventKind(StrEnum):
    STARTED = "started"
    STEP_STARTED = "step_started"
    STEP_COMPLETED = "step_completed"
    STEP_SKIPPED = "step_skipped"
    WAITING = "waiting"
    ANSWERED = "answered"
    EMAIL_SENT = "email_sent"
    EMAIL_RECEIVED = "email_received"
    NOTIFIED = "notified"
    #: A write the switches did not allow, recorded as what *would* have
    #: happened so the run stays auditable and the person is not misled.
    HELD = "held"
    RETRIED = "retried"
    ERROR = "error"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class FileSource(StrEnum):
    #: Downloaded from the subject task's attachments.
    SHAREPOINT = "sharepoint"
    #: Uploaded by the person while answering a step.
    UPLOAD = "upload"
    #: Came in on a supplier's reply.
    EMAIL = "email"
    #: Fetched from Zoho Books once the quote exists there.
    ZOHO = "zoho"


class WorkflowSettings(Base, Timestamped):
    """The switches. One row; ``id`` is fixed at 1.

    Each one gates a class of side effect the module can have on the world
    outside this database. They ship off so that a freshly deployed flow
    prepares mails, quotes and attachments and does nothing with them until an
    administrator has read what it prepared and decided.
    """

    __tablename__ = "workflow_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    #: Whether the ``email`` step may actually send. Off: the mail is
    #: composed, recorded on the run as held, and the run carries on.
    send_email: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    #: Whether ``sharepoint_attach`` may add files to a Proposals task.
    write_sharepoint: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    #: Whether ``zoho_create`` may create an estimate in Zoho Books.
    write_zoho: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    #: Which mailbox request-for-quote mails go out from. Null means the
    #: intake mailbox, which is the right default: that is the box the worker
    #: reads, so a supplier's reply is seen without anybody forwarding it.
    from_mailbox: Mapped[str | None] = mapped_column(String(320))
    #: How often the worker looks at runs that are waiting on the world.
    poll_seconds: Mapped[int] = mapped_column(
        Integer, default=60, server_default=text("60"), nullable=False
    )
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


class Workflow(Base, UUIDPrimaryKey, Timestamped):
    """A process, as an ordered list of blocks."""

    __tablename__ = "workflows"

    #: Short, stable, URL-safe. What a run refers to and what the seed keys on.
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    #: The team whose work this is. Null means any team may start it.
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="SET NULL"), index=True
    )
    #: What a run is about. Only ``proposal_task`` exists today; the column is
    #: here so a second kind is a value rather than a migration.
    subject_kind: Mapped[str] = mapped_column(
        String(40), default="proposal_task", server_default=text("'proposal_task'"),
        nullable=False,
    )
    trigger: Mapped[str] = mapped_column(
        String(24), default=WorkflowTrigger.MANUAL, server_default=text("'manual'"),
        nullable=False,
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    #: ``[{key, kind, name, config, when}]`` — see ``app.workflows.catalogue``
    #: for what each kind's ``config`` holds. Validated on write against that
    #: catalogue, so a run never meets a block it does not know.
    steps: Mapped[list[Any]] = mapped_column(JSONB, default=list, nullable=False)
    #: Bumped on every edit to the steps. A run remembers the version it began
    #: on, so an edit made mid-run changes the next run and not this one.
    version: Mapped[int] = mapped_column(
        Integer, default=1, server_default=text("1"), nullable=False
    )
    #: Seeded from the catalogue. Kept editable — a super admin may change
    #: every step — but not deletable, so the shipped flow can always be
    #: restored by name.
    is_system: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    team: Mapped[Team | None] = relationship(lazy="joined")

    def __repr__(self) -> str:
        return f"<Workflow {self.key} v{self.version}>"


class WorkflowRun(Base, UUIDPrimaryKey, Timestamped):
    """One execution of a workflow for one subject."""

    __tablename__ = "workflow_runs"
    __table_args__ = (
        Index("ix_workflow_runs_subject", "subject_kind", "subject_id"),
        Index("ix_workflow_runs_status_wake", "status", "wake_at"),
        Index("ix_workflow_runs_owner_started", "owner_id", "started_at"),
    )

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False,
        index=True,
    )
    workflow_version: Mapped[int] = mapped_column(Integer, nullable=False)
    #: A copy of the steps as they were when the run began. The definition
    #: may be edited underneath a run that takes three weeks; the copy is what
    #: this run follows.
    steps: Mapped[list[Any]] = mapped_column(JSONB, default=list, nullable=False)

    subject_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    #: The Proposals task id, as text.
    subject_id: Mapped[str] = mapped_column(String(120), nullable=False)
    subject_label: Mapped[str | None] = mapped_column(String(300))
    #: Whose run this is. Steps that call a route call it as this person, so
    #: what the run may read and change is what they may.
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="SET NULL"), index=True
    )
    #: Short and unique, ``HZ-7K3QD2``. Goes in the subject of every mail the
    #: run sends, and is how a reply weeks later is known to be about it.
    tag: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)

    status: Mapped[str] = mapped_column(
        String(24), default=RunStatus.RUNNING, server_default=text("'running'"),
        nullable=False,
    )
    #: Index into ``steps`` of the step the run is on or stopped at.
    step_index: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    #: Everything the steps have produced, by ``save_as`` key.
    context: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    #: What the run is waiting on, rendered for the person: a question with
    #: its fields, or a review with the thing to review. Null when running.
    pending: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    #: When the worker should next look at a run waiting on the world.
    wake_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: When a wait gives up, if it does.
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    cancelled_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    #: Money spent on models by this run, in USD.
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), default=Decimal(0), server_default=text("0"), nullable=False
    )

    workflow: Mapped[Workflow] = relationship(lazy="joined")
    owner: Mapped[User] = relationship(foreign_keys=[owner_id], lazy="joined")
    events: Mapped[list[WorkflowRunEvent]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="WorkflowRunEvent.seq"
    )
    files: Mapped[list[WorkflowRunFile]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="WorkflowRunFile.created_at"
    )
    messages: Mapped[list[WorkflowRunMessage]] = relationship(
        back_populates="run", cascade="all, delete-orphan",
        order_by="WorkflowRunMessage.created_at",
    )

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_RUN_STATUSES

    @property
    def current_step(self) -> dict[str, Any] | None:
        if 0 <= self.step_index < len(self.steps or []):
            return self.steps[self.step_index]
        return None

    def __repr__(self) -> str:
        return f"<WorkflowRun {self.tag} {self.status} step={self.step_index}>"


class WorkflowRunEvent(Base, UUIDPrimaryKey, Timestamped):
    """One thing that happened on a run, in order. The audit trail."""

    __tablename__ = "workflow_run_events"
    __table_args__ = (Index("ix_workflow_run_events_run_seq", "run_id", "seq", unique=True),)

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    step_key: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    run: Mapped[WorkflowRun] = relationship(back_populates="events")


class WorkflowRunFile(Base, UUIDPrimaryKey, Timestamped):
    """A document a run holds: read from the task, uploaded, received, fetched.

    Bytes in the row, like supplier quotes in the comparison module: these are
    a few documents per run, they must survive the mailbox and the task being
    tidied, and a blob store would be a second thing to deploy.
    """

    __tablename__ = "workflow_run_files"

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False,
        index=True,
    )
    step_key: Mapped[str | None] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(120))
    size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    #: Who sent it, for a file that came by mail; the supplier's name when known.
    origin: Mapped[str | None] = mapped_column(String(300))
    #: Free-form: which supplier, which message, what it was read as.
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    run: Mapped[WorkflowRun] = relationship(back_populates="files")


class WorkflowRunMessage(Base, UUIDPrimaryKey, Timestamped):
    """A mail the run sent, or one it recognised as a reply."""

    __tablename__ = "workflow_run_messages"

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False,
        index=True,
    )
    step_key: Mapped[str | None] = mapped_column(String(64))
    #: ``out`` or ``in``.
    direction: Mapped[str] = mapped_column(String(4), nullable=False)
    #: The supplier or party this was to or from, as the run knows them.
    party: Mapped[str | None] = mapped_column(String(300))
    address: Mapped[str | None] = mapped_column(String(320))
    subject: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    #: ``sent``, ``held`` (switch off), ``failed`` for outbound; ``received``
    #: for inbound.
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    intake_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("intake_messages.id", ondelete="SET NULL")
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    run: Mapped[WorkflowRun] = relationship(back_populates="messages")
