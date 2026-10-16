"""Each person's own AI agents, and what they did when they ran.

An agent is what its owner made in the builder: a name, a face, the job in
their own words, and the job as an ordered list of blocks. The blocks are kept
as JSON exactly as the frontend shapes them (``{id, kind, config}``) — the
catalogue of kinds lives in ``app.agents.engine``, and a list the engine can
read is checked there on save, not by the column.

A run is one go at the job, on one Proposals task. It copies the agent's steps
when it starts, so editing an agent mid-run changes the next run, not this
one, and an old run still says what it was asked to do. Its ``results`` hold
one entry per step — what happened, what it produced, and where every fact
came from — and are what the run page draws.

Table names are ``user_agents`` / ``agent_runs`` on purpose: the removed AI
employees module left ``ai_employees`` behind, undropped.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, LargeBinary, Numeric, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamped, UUIDPrimaryKey


class UserAgent(Base, UUIDPrimaryKey, Timestamped):
    __tablename__ = "user_agents"

    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    role: Mapped[str] = mapped_column(String(200), nullable=False, server_default=text("''"))
    avatar: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    instructions: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    steps: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    #: Set on an agent the app gives people rather than one they made —
    #: "nova", the presales team's (``app.agents.nova``). Its job is the
    #: app's, the same for everyone, and its owner can only switch it off.
    system_key: Mapped[str | None] = mapped_column(String(32))
    #: Whether its trigger starts it on its own ("A task is assigned to me").
    #: Off, it runs only when its owner presses Run.
    always_on: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    #: What the trigger has already seen, e.g. ``{"seen": [task ids]}``, so a
    #: task starts it once. Kept by ``app.agents.worker``.
    trigger_state: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    #: The agent's face as a PNG, drawn by the browser from ``avatar``, for
    #: Teams cards — they can only show a picture fetched from a public URL.
    #: ``face_key`` names the avatar it was drawn from, so a new face is drawn
    #: when the avatar changes, and it busts Teams' image cache.
    face_png: Mapped[bytes | None] = mapped_column(LargeBinary, deferred=True)
    face_key: Mapped[str | None] = mapped_column(String(120))

    __table_args__ = (
        Index("ix_user_agents_owner", "owner_id"),
        Index("uq_user_agents_owner_system", "owner_id", "system_key", unique=True,
              postgresql_where=text("system_key IS NOT NULL")),
    )


class AgentRun(Base, UUIDPrimaryKey, Timestamped):
    __tablename__ = "agent_runs"

    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user_agents.id", ondelete="CASCADE"), nullable=False
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    #: The Proposals task it ran on. None when it ran on uploaded files alone.
    task_id: Mapped[str | None] = mapped_column(String(64))
    task_title: Mapped[str | None] = mapped_column(Text)

    #: running | waiting | done | failed | stopped
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'running'"))
    #: What it was asked to do: ``{name, instructions, steps}`` at the start.
    plan: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    #: One entry per step, in order; see ``app.agents.engine.StepResult``.
    results: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    #: What it is waiting for the owner to answer, while ``status`` is waiting.
    question: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    #: The result in a sentence or two — always set when a run ends.
    summary: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)

    model: Mapped[str | None] = mapped_column(String(60))
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    web_searches: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False, server_default=text("0"))

    #: hand — its owner pressed Run; trigger — it started on its own.
    started_by: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'hand'"))
    #: Touched by the process running it, every half minute while it waits.
    #: A live run whose beat has stopped was cut off (a restart, a deploy) —
    #: which any process can tell, not only the one that ran it.
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_agent_runs_agent", "agent_id", "created_at"),
        Index("ix_agent_runs_owner", "owner_id"),
    )


class AgentRfq(Base, UUIDPrimaryKey, Timestamped):
    """One RFQ an agent sent for its owner, and what became of it.

    Written when "Send the emails" sends; watched from then on, outside the
    run, so a reply that comes days later still finds it: the reply is
    noticed in the owner's inbox (``replied``), they say whether to take the
    supplier's quote (``taking`` → ``taken``, onto a quote request with the
    costing report made), and whether to send that for approval
    (``submitted``). ``declined`` when they say no; ``failed`` when a step
    could not be done, with ``note`` saying why.
    """

    __tablename__ = "agent_rfqs"

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user_agents.id", ondelete="CASCADE"), nullable=False
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[str | None] = mapped_column(String(64))
    task_title: Mapped[str | None] = mapped_column(Text)
    supplier_name: Mapped[str | None] = mapped_column(Text)
    #: Who it was written to, and where it really went — the test inbox, in test mode.
    supplier_email: Mapped[str] = mapped_column(String(320), nullable=False)
    sent_to: Mapped[str] = mapped_column(String(320), nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: waiting | replied | taking | taken | declined | submitted | failed
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'waiting'"))
    #: The reply: message id, from, subject, received, preview, attachment names.
    reply: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    quote_request_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("quote_requests.id", ondelete="SET NULL")
    )
    #: The costing report in a few figures, once made.
    report: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    #: The last thing that happened, in words.
    note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_agent_rfqs_run", "run_id"),
        Index("ix_agent_rfqs_status", "status"),
    )
