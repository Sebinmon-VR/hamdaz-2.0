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

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, String, Text, text
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

    __table_args__ = (Index("ix_user_agents_owner", "owner_id"),)


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

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_agent_runs_agent", "agent_id", "created_at"),
        Index("ix_agent_runs_owner", "owner_id"),
    )
