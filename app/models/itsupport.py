"""IT Support in Teams: its settings, changed on the admin page.

One table, ``itsupport_settings``, one row (``id`` is always 1): the switch,
whose chats it answers and what to call them, the due-today check-ins, the
brain's model, and what it may do in each ERP module. Until a super admin
saves the page, the row is seeded from the IT_SUPPORT_* environment values,
so turning the page on changes nothing by itself.

The machine that runs the agent still needs IT_SUPPORT_CHAT_ENABLED and the
account's sign-in (``python -m app.itsupport.connect``); this row is the
company's switch, that one the machine's. The worker writes its heartbeat
here too (which machine, when, signed in as whom, the last problem), so the
page shows whether it is running — and a second machine sees the first one
and stays quiet instead of answering twice.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamped


class ITSupportSettings(Base, Timestamped):
    """The single settings row (``id`` is always 1)."""

    __tablename__ = "itsupport_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    #: Off: no chat is answered and no check-in sent, on any machine.
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    #: Whose chats it answers: ``[{"email": ..., "name": ...}]``. ``name`` is
    #: what to call them when their Teams name says otherwise; blank is the
    #: first word of their Teams name.
    people: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    #: End each reply with how long it took and which model wrote it.
    show_timing: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    # ── due-today check-ins ──
    due_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    #: Office hours, IST ("HH:MM"), and working days (Monday = 0).
    due_from: Mapped[str] = mapped_column(String(5), nullable=False, default="09:00",
                                          server_default="09:00")
    due_until: Mapped[str] = mapped_column(String(5), nullable=False, default="17:00",
                                           server_default="17:00")
    due_days: Mapped[list[int]] = mapped_column(
        ARRAY(Integer), nullable=False, default=list,
        server_default=text("'{0,1,2,3,4,5}'::integer[]"),
    )
    #: The morning brief, and when it goes (HH:MM, IST).
    brief_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    brief_at: Mapped[str] = mapped_column(String(5), nullable=False, default="09:30",
                                          server_default="09:30")
    #: Every check-in and brief sent into this one person's chat instead, for a trial.
    due_test_to: Mapped[str] = mapped_column(String(320), nullable=False, default="",
                                             server_default="")

    # ── the brain ──
    brain_model: Mapped[str] = mapped_column(String(60), nullable=False,
                                             default="claude-haiku-5-5",
                                             server_default="claude-haiku-5-5")
    brain_effort: Mapped[str] = mapped_column(String(10), nullable=False, default="high",
                                              server_default="high")
    #: Over the defaults in app/itsupport/erp.py MODES: ``{"hr": "off"}``.
    #: Finance and payroll stay off whatever this says.
    module_modes: Mapped[dict[str, str]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    #: The feature switches (``app.itsupport.features``): ``{"status_updates":
    #: true}``. A feature not named here is at its default, which is off for
    #: anything that sends a message unasked.
    features: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    # ── the account's sign-in, made with Connect on the page ──
    #: ``graph.sign_in_record``: the refresh token encrypted with a key from
    #: SESSION_SECRET, and whose it is. None: not connected from the web (a
    #: laptop may still hold one in its own file).
    sign_in: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    connected_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    # ── the worker's heartbeat: the lease of the one process answering ──
    #: The machine answering, and when it last checked in.
    running_on: Mapped[str | None] = mapped_column(String(200))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: The account its sign-in is for (should be itsupport@).
    signed_in_as: Mapped[str | None] = mapped_column(String(320))
    last_error: Mapped[str | None] = mapped_column(Text)

    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


class ITSupportActivity(Base):
    """What the agent did, one row each: a change it made in the ERP for
    somebody, an answer filed, a message it sent unasked, a 👎 on one of its
    replies, a turn's model spend. Shown on the settings page; the audit
    trail for everything it writes."""

    __tablename__ = "itsupport_activity"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                          default=uuid.uuid4)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                         server_default=text("now()"), index=True)
    #: Whose chat it was for.
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    #: change, answer, sent, feedback, turn.
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True,
                                     server_default=text("true"))
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 5))


class ITSupportSent(Base):
    """Something the agent sends once (a daily ask, an alert), by a key that
    names it: ``status-ask:sebin@…:2026-11-18``. In the database so a
    redeploy doesn't send it again."""

    __tablename__ = "itsupport_sent"

    key: Mapped[str] = mapped_column(String(300), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                              server_default=text("now()"), index=True)
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
