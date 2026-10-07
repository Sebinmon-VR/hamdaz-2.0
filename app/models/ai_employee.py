"""AI employees: named workers built on the assistant, each with its own job and rules.

An AI employee is the assistant's engine — the same tools, the same permission
checks, the same confirmations — wearing a job description. A super admin
creates one and decides:

* **who it is**: a name, a job title, what it does;
* **its rules**: instructions it must follow, written in plain language;
* **its brain**: which model, and how hard it thinks;
* **its reach**: which ERP modules it may use, and whether it may change
  anything (never; only after the person confirms; or as the assistant's
  own policy allows);
* **who may talk to it**: everyone with the assistant, or only some roles;
* **what it may spend**: a monthly cap on the cost of its conversations.

A conversation with one is an ordinary assistant conversation marked with the
employee (``subject_kind = "employee"``), so its history, runs, cost and audit
trail are the assistant's own. It acts as the person talking to it, never with
more access than they have.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import Boolean, ForeignKey, Integer, Numeric, String, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamped, UUIDPrimaryKey


class WriteMode:
    """What an AI employee may change."""

    #: Look things up only; every tool that writes is withheld.
    READ_ONLY = "read_only"
    #: Writes are offered, and every one waits for the person to say yes.
    CONFIRM = "confirm"
    #: As the assistant's own policy says, tool by tool.
    POLICY = "policy"

    ALL = (READ_ONLY, CONFIRM, POLICY)


class AIEmployee(Base, UUIDPrimaryKey, Timestamped):
    __tablename__ = "ai_employees"

    name: Mapped[str] = mapped_column(String(80), nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    #: What it does, as its manager would describe the job.
    description: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    #: Rules it must follow, in plain language, one per line.
    instructions: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    #: The first thing it says in a new chat; blank for a default greeting.
    greeting: Mapped[str | None] = mapped_column(Text)
    #: A colour for its initials on screen.
    color: Mapped[str] = mapped_column(String(16), nullable=False, default="#0e5e80", server_default="#0e5e80")

    #: Blank follows the assistant's own model and effort.
    model_key: Mapped[str | None] = mapped_column(String(64))
    reasoning_effort: Mapped[str | None] = mapped_column(String(16))

    #: Module keys it may use. Empty means every module the assistant offers.
    allowed_modules: Mapped[list[str]] = mapped_column(
        ARRAY(String(40)), nullable=False, default=list, server_default=text("'{}'::varchar[]")
    )
    write_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default=WriteMode.READ_ONLY, server_default=WriteMode.READ_ONLY
    )
    #: Global roles that may talk to it. Empty means everyone with the assistant.
    audience_roles: Mapped[list[str]] = mapped_column(
        ARRAY(String(40)), nullable=False, default=list, server_default=text("'{}'::varchar[]")
    )
    #: Stops new turns once its conversations have cost this much this month.
    monthly_budget_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))

    #: Its own Microsoft 365 account (e.g. luna@hamdaz.com), for Teams and
    #: Outlook. Connected separately — see app/models/teams_chat.py.
    ms_account_email: Mapped[str | None] = mapped_column(String(320))
    #: Answer Teams chats as that account.
    teams_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))

    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
