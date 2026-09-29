"""Tenders read from the Ariba supplier portal, and the reader's own state.

``ariba_event`` holds one row per event the Events list has shown us, keyed by
Ariba's own document id. Only what is needed: the title, the End Time (the
due date) and the status group it was listed under. The tender number is
pulled out of the title so a Proposals row can be matched to it.

``ariba_state`` is one row, ``id`` fixed at 1, like ``ZohoToken``: the browser
session being reused, and what the reader last did. The session is a live
credential — never log it, and never put it in a response.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import Boolean, Date, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamped, UUIDPrimaryKey


class AribaEvent(Base, Timestamped):
    __tablename__ = "ariba_event"

    #: ``Doc338825897`` — Ariba's id, stable for the life of the event.
    doc_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    #: The tender number found in the title, if any. How a Proposals row is
    #: matched to its event.
    reference: Mapped[str | None] = mapped_column(String(40), index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    #: The End Time column: when the tender closes.
    end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: ``Open`` while the Events list shows it under Status: Open; ``Closed``
    #: once a later visit no longer finds it there.
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    #: The Participated column as last read: whether Ariba holds our response.
    participated: Mapped[bool | None] = mapped_column(Boolean)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AribaState(Base, Timestamped):
    __tablename__ = "ariba_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    #: Playwright's storage state — the portal's cookies. Reused until Ariba
    #: expires it, so most visits do not sign in at all.
    session_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    #: Proposals rows created after this have not been looked for yet.
    watermark: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_visit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: What the last visit found, or why it failed.
    last_result: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    #: No longer set — the timed pause it held became ``blocked_at``, which
    #: does not expire. Kept so the column's history reads.
    paused_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Set when a sign-in fails, for any reason. Nothing signs in again until a
    #: super admin resumes it — a wrong password retried on a timer is how an
    #: account gets locked, and a security check retried looks like an attack.
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    blocked_reason: Mapped[str | None] = mapped_column(Text)
    #: Set when a super admin stops the reader from the admin page: no visits
    #: and no BCD corrections until one of them starts it again.
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stopped_by: Mapped[str | None] = mapped_column(String(320))
    visits_on: Mapped[date | None] = mapped_column(Date)
    visits_today: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Sign-ins, counted apart from visits: a visit on the saved session is
    #: not one. Attempts count, successful or not.
    logins_on: Mapped[date | None] = mapped_column(Date)
    logins_today: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    def __repr__(self) -> str:
        # Deliberately no session material.
        return f"<AribaState last_visit_at={self.last_visit_at}>"


class AribaBcdFix(Base, UUIDPrimaryKey, Timestamped):
    """A Proposals row whose BCD disagreed with Ariba, and what was done.

    ``applied`` false is a preview — the write is off, or it failed (``error``).
    Previews are replaced on every check; applied rows are kept as the record
    of what this app changed in the list.
    """

    __tablename__ = "ariba_bcd_fix"

    item_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    doc_id: Mapped[str] = mapped_column(String(40), nullable=False)
    reference: Mapped[str] = mapped_column(String(40), nullable=False)
    task_title: Mapped[str] = mapped_column(Text, nullable=False)
    #: As SharePoint holds them — UTC strings, ``2026-10-05T16:00:00Z``.
    old_bcd: Mapped[str | None] = mapped_column(String(40))
    new_bcd: Mapped[str] = mapped_column(String(40), nullable=False)
    #: The Ariba End Time the new value was taken from.
    ariba_end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error: Mapped[str | None] = mapped_column(Text)
