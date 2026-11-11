"""Enquiries sent from the contact form on hamdaz.com.

**The visitor is not a user.** Like a job candidate, somebody filling in the
website's form has no account, no session and no row in ``users``: the row
carries the contact details they typed and nothing else. The public endpoint
that writes it is mounted outside the API prefix with no authentication at all —
see ``app.website.public``.

**Where they came from is kept apart from what they said.** ``attribution``
holds the campaign tags and Google's click id exactly as the landing page
received them, so the question "which ads bring enquiries" can be answered from
the rows themselves later, without anybody having to remember to ask the visitor.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Timestamped, UUIDPrimaryKey
from app.models.user import User


class WebsiteEnquiryStatus(StrEnum):
    """Where an enquiry stands with sales. Set by a person, never by the code."""

    NEW = "new"
    CONTACTED = "contacted"
    QUALIFIED = "qualified"
    WON = "won"
    LOST = "lost"
    #: Got past the honeypot but is plainly not a customer. Kept rather than
    #: deleted, so a pattern in what gets through can be seen and blocked.
    SPAM = "spam"


class WebsiteEnquiry(Base, UUIDPrimaryKey, Timestamped):
    """One message from the website's contact form."""

    __tablename__ = "website_enquiries"
    #: The list is always newest first.
    __table_args__ = (Index("ix_website_enquiries_created_at", "created_at"),)

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    #: At least one of email and phone is present; the endpoint refuses a
    #: message there is no way to answer.
    email: Mapped[str | None] = mapped_column(String(320), index=True)
    phone: Mapped[str | None] = mapped_column(String(40))
    company: Mapped[str | None] = mapped_column(String(200))
    #: The "I'm interested in" boxes they ticked, in the form's own wording.
    topics: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)

    #: The site path the form was sent from.
    page: Mapped[str | None] = mapped_column(String(500))
    #: The first page of the visit, path and query — usually where an ad landed.
    landing: Mapped[str | None] = mapped_column(String(500))
    referrer: Mapped[str | None] = mapped_column(String(500))
    #: utm_source, utm_medium, utm_campaign, utm_term, utm_content and gclid —
    #: only the ones the visit carried.
    attribution: Mapped[dict[str, str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )

    # str, not the enum: the column is a plain String, as elsewhere, so a row
    # read back from Postgres carries a str rather than the member.
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, index=True, server_default=WebsiteEnquiryStatus.NEW.value
    )
    assigned_to_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    notes: Mapped[str | None] = mapped_column(Text)
    #: When somebody first moved it on from "new", and who. Kept once set: it
    #: answers "how long did that wait for a reply", which a later status
    #: change must not rewrite.
    handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    handled_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    assigned_to: Mapped[User | None] = relationship(foreign_keys=[assigned_to_id], lazy="joined")
    handled_by: Mapped[User | None] = relationship(foreign_keys=[handled_by_id], lazy="joined")
