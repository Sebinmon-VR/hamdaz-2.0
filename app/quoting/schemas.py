"""Request and response shapes for quote requests.

The field names mirror a Zoho Books estimate on purpose — ``customer_name``,
``reference_number``, ``expiry_date``, items with ``rate`` — so the eventual
integration is a mapping rather than a translation. ``cf_bcd`` and ``cf_portal``
keep Zoho's own custom-field keys.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.quoting import CommentTarget, QuoteStatus, ReviewAction
from app.proposals.schemas import TaskOut


class ItemIn(BaseModel):
    """One priced line. ``rate`` is the unit price, as Zoho calls it."""

    name: str = Field(min_length=1, max_length=500)
    description: str | None = None
    item_code: str | None = Field(default=None, max_length=120)
    brand: str | None = Field(default=None, max_length=120)
    unit: str | None = Field(default=None, max_length=40)
    quantity: Decimal = Field(default=Decimal(1), ge=0)
    rate: Decimal = Field(default=Decimal(0), ge=0)
    discount: Decimal = Field(default=Decimal(0), ge=0)
    tax_name: str | None = Field(default=None, max_length=60)
    tax_percentage: Decimal | None = Field(default=None, ge=0, le=100)
    #: What the line costs us, so margin is visible during review.
    cost_rate: Decimal | None = Field(default=None, ge=0)
    source_supplier_quote_id: uuid.UUID | None = None


class QuoteRequestIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    customer_name: str = Field(min_length=1, max_length=200)
    customer_id: str | None = Field(default=None, max_length=60)
    contact_person: str | None = Field(default=None, max_length=200)
    reference: str | None = Field(default=None, max_length=60)
    #: The customer's own reference — their PO or enquiry number.
    reference_number: str | None = Field(default=None, max_length=120)
    quote_date: date | None = None
    expiry_date: date | None = None
    currency: str = Field(default="AED", min_length=3, max_length=3)
    salesperson_name: str | None = Field(default=None, max_length=200)
    place_of_supply: str | None = Field(default=None, max_length=120)
    payment_terms: str | None = Field(default=None, max_length=200)
    delivery_terms: str | None = Field(default=None, max_length=200)
    #: Bid closing date — Zoho's ``cf_bcd``, the Proposals list's BCD.
    cf_bcd: date | None = None
    cf_portal: str | None = Field(default=None, max_length=120)
    subject: str | None = None
    notes: str | None = None
    terms: str | None = None

    discount: Decimal = Field(default=Decimal(0), ge=0)
    shipping_charge: Decimal = Field(default=Decimal(0), ge=0)
    adjustment: Decimal = Decimal(0)

    #: Turn on when several suppliers quoted the same requirement. The comparison
    #: and the "which supplier won" decision only mean anything when it is set.
    multiple_supplier_quotes: bool = False
    items: list[ItemIn] = Field(default_factory=list)


class ItemOut(ItemIn):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    position: int
    line_total: Decimal
    #: Null unless a cost is known for the line.
    margin: Decimal | None


class ReviewOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    action: ReviewAction
    note: str | None
    #: Which round it was written in, so an old note is visibly old.
    revision: int
    reviewer_name: str | None = None
    selected_supplier_quote_id: uuid.UUID | None
    created_at: datetime


class CommentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    target_type: CommentTarget
    #: A field name, a line id, or a supplier quote id. Null for the whole quote.
    target_ref: str | None
    body: str
    revision: int
    author_name: str | None = None
    created_at: datetime
    resolved_at: datetime | None
    is_open: bool


class RevisionOut(BaseModel):
    """A round that ended, exactly as it stood when it did."""

    model_config = ConfigDict(from_attributes=True)

    revision: int
    #: What ended it: approved, rejected, rework, or superseded when an approver
    #: repriced the quote from a different supplier.
    outcome: str
    #: The form, its lines, its totals, the supplier it was priced from and the
    #: win probability it carried. Denormalised, because the point of it is to
    #: show what was true then rather than what is true now.
    snapshot: dict[str, Any]
    created_at: datetime


class QuoteRequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    reference: str | None
    title: str
    status: QuoteStatus
    revision: int

    customer_name: str
    customer_id: str | None
    contact_person: str | None
    reference_number: str | None
    quote_date: datetime | None
    expiry_date: datetime | None
    currency: str
    salesperson_name: str | None
    place_of_supply: str | None
    payment_terms: str | None
    delivery_terms: str | None
    cf_bcd: datetime | None
    cf_portal: str | None
    subject: str | None
    notes: str | None
    terms: str | None

    discount: Decimal
    shipping_charge: Decimal
    adjustment: Decimal
    sub_total: Decimal
    #: Computed here, never accepted from the caller.
    total: Decimal

    multiple_supplier_quotes: bool
    comparison_id: uuid.UUID | None
    selected_supplier_quote_id: uuid.UUID | None
    #: 0..1. Read ``win_basis`` before trusting it — a probability without its
    #: sample size invites more confidence than it has earned.
    win_probability: Decimal | None
    win_basis: dict[str, Any] | None

    #: The Proposals row this was raised from, when it was raised from one.
    source_task_id: str | None = None
    source_task_url: str | None = None

    team_id: uuid.UUID
    team_name: str | None = None
    created_by_name: str | None = None
    assigned_to_name: str | None = None
    submitted_at: datetime | None
    decided_at: datetime | None
    #: When the approvers were emailed about this, and what stopped it if they
    #: were not. A quote can be waiting for approval that nobody was told about,
    #: and the person who sent it should be able to see that.
    approvers_notified_at: datetime | None = None
    notify_error: str | None = None
    created_at: datetime
    updated_at: datetime

    items: list[ItemOut]
    reviews: list[ReviewOut]
    comments: list[CommentOut]
    #: Every round that has ended, oldest first. What a negotiation is argued
    #: over: the prices that were quoted before, beside the ones being quoted
    #: now, with the win probability each round carried.
    revisions: list[RevisionOut] = Field(default_factory=list)

    #: The comparison of the supplier quotes behind this one, when there is one.
    comparison: dict[str, Any] | None = None
    #: Whether the caller may edit, may send it for approval, and may decide.
    may_edit: bool = False
    #: Set once it is priced from a supplier and has lines. ``submit_reason``
    #: says what is missing while it is not, so a form can say why the button is
    #: off instead of only finding out when it is pressed.
    may_submit: bool = False
    submit_reason: str | None = None
    may_approve: bool = False
    approve_reason: str | None = None

    @field_validator("comparison", mode="before")
    @classmethod
    def _the_analysis_not_the_row(cls, value: Any) -> dict[str, Any] | None:
        """Read from the ORM, ``comparison`` is the whole comparison record.

        What a caller wants from it is what it concluded, so the conversion
        belongs here rather than in each of the several routes that build this
        body — one of which forgot, which is how a saved comparison turned an
        attached quote into a 500.
        """
        if value is None or isinstance(value, dict):
            return value
        return getattr(value, "analysis", None)


class QuoteSummaryOut(BaseModel):
    """A list row — no items, reviews or comparison."""

    id: uuid.UUID
    reference: str | None
    title: str
    customer_name: str
    status: QuoteStatus
    revision: int
    currency: str
    total: Decimal
    win_probability: Decimal | None
    multiple_supplier_quotes: bool
    created_by_name: str | None
    assigned_to_name: str | None
    open_comments: int
    created_at: datetime


class QuotableTaskOut(TaskOut):
    """A Proposals row of the caller's, and the quote already raised from it.

    The task itself is the Proposals module's own shape, inherited rather than
    copied: the quoting page shows the same enquiry the proposals page does, and
    two definitions of one row would drift the first time a column is added.

    The quote fields are what this adds. Without them the list offers to raise a
    second quote against an enquiry that already has one, and the person
    choosing has no way of knowing — which is two quotes for somebody
    downstream and an argument for somebody else.
    """

    quote_request_id: uuid.UUID | None = None
    quote_reference: str | None = None
    quote_status: QuoteStatus | None = None
    #: Where it is, in words, for a list that has to be readable at a glance.
    quote_title: str | None = None

    @classmethod
    def of(cls, task: Any, quote: Any | None) -> QuotableTaskOut:
        return cls(
            **TaskOut.from_domain(task).model_dump(),
            quote_request_id=quote.id if quote else None,
            quote_reference=quote.reference if quote else None,
            quote_status=quote.status if quote else None,
            quote_title=quote.title if quote else None,
        )


class QuotableTasksOut(BaseModel):
    """The caller's tasks, and enough context to read an empty list correctly.

    ``in_sharepoint`` is the difference between "you have nothing outstanding"
    and "we cannot see your work at all", which are not the same message.
    """

    email: str
    in_sharepoint: bool
    #: Everything assigned to them, before the scope is applied.
    total: int
    #: Not finished, closed bids included.
    open_count: int
    #: Not finished and the bid has not closed — what can still be quoted for.
    live_count: int
    #: How many already have a quote raised against them.
    quoted_count: int
    tasks: list[QuotableTaskOut]


class TaskQuoteIn(BaseModel):
    """Which of the caller's Proposals tasks to raise a quote for."""

    task_id: str = Field(min_length=1, max_length=120)


class NegotiationIn(BaseModel):
    """What the customer came back with."""

    note: str = Field(min_length=1)


class SupplierChoiceIn(BaseModel):
    """Which supplier's offer this quote is priced from."""

    supplier_quote_id: uuid.UUID
    #: Added to the supplier's cost to get the selling rate. 0 prices the job at
    #: cost, which is a real answer and a visible one — every rate can be edited
    #: line by line afterwards.
    markup_percent: Decimal = Field(default=Decimal(0), ge=0, le=1000)


class ReviewIn(BaseModel):
    action: ReviewAction
    #: Required for a rejection or a rework — a refusal that does not say why is
    #: the thing people complain about.
    note: str | None = None
    #: Required when approving a quote with several supplier offers: choosing the
    #: supplier is the decision being approved.
    selected_supplier_quote_id: uuid.UUID | None = None


class CommentIn(BaseModel):
    body: str = Field(min_length=1)
    target_type: CommentTarget = CommentTarget.QUOTE
    #: A field name, a line id, or a supplier quote id.
    target_ref: str | None = Field(default=None, max_length=120)
