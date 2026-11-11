"""Request and response shapes for website enquiries.

The visitor sees one sentence back and nothing else — there is no public
response model with an id in it, so a visitor cannot learn that a row exists,
let alone address it. Everything else here is for colleagues.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.website import WebsiteEnquiryStatus


class PublicThanksOut(BaseModel):
    message: str


class PersonOut(BaseModel):
    """Somebody on the staff side, in the least that identifies them."""

    id: uuid.UUID
    name: str
    email: str


class EnquirySummaryOut(BaseModel):
    id: uuid.UUID
    created_at: datetime
    name: str
    company: str | None
    email: str | None
    phone: str | None
    topics: list[str]
    #: The first line or so of the message, so the list can be read without
    #: opening every row.
    preview: str
    status: WebsiteEnquiryStatus
    assigned_to: PersonOut | None


class EnquiryPage(BaseModel):
    enquiries: list[EnquirySummaryOut]
    total: int
    #: How many there are in each status, whatever the filter — what the
    #: filter's own labels show.
    counts: dict[str, int]


class EnquiryOut(EnquirySummaryOut):
    message: str
    page: str | None
    landing: str | None
    referrer: str | None
    attribution: dict[str, str]
    notes: str | None
    handled_at: datetime | None
    handled_by: PersonOut | None
    updated_at: datetime


class EnquiryUpdateIn(BaseModel):
    """Only the fields sent are changed. ``assigned_to_id: null`` unassigns."""

    status: WebsiteEnquiryStatus | None = None
    assigned_to_id: uuid.UUID | None = None
    notes: str | None = Field(default=None, max_length=10_000)
