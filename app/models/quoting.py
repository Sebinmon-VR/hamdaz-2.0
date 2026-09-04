"""Quote requests: what presales fills in, and the approval it goes through.

A *quote request* is a quote before it exists anywhere official. Somebody in
presales fills in the form, attaches the supplier quotes they were sent, picks
the one to price the quote from, and puts it up for approval. Approvers comment,
ask for changes, and eventually approve — on that supplier's offer or, if they
disagree, on another one, which reprices the quote. Only then does it join a queue for
being created in Zoho — which nothing here does. **Zoho is live and this module
never writes to it.**

The field names deliberately mirror a Zoho Books estimate — ``customer_name``,
``reference_number``, ``expiry_date``, ``line_items`` with ``rate`` — so the
eventual integration is a mapping rather than a translation. Where Zoho uses a
custom field this does too, by the same key: ``cf_bcd`` is the bid closing date
the Proposals list already calls BCD.

Four tables, because four different things change at different times:

* ``quote_requests`` — the form and where it is in the workflow.
* ``quote_request_items`` — the priced lines, which is what a quote *is*.
* ``quote_reviews`` — one approver's decision, appended and never edited. An
  approval history that can be rewritten is not a history.
* ``quote_comments`` — remarks anchored to a particular field, line or supplier
  quote, so "this rate looks wrong" points at the rate rather than floating at
  the bottom of the page.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Timestamped, UUIDPrimaryKey
from app.models.comparison import QuoteComparison
from app.models.team import Team
from app.models.user import User


class QuoteStatus(StrEnum):
    """Where a request is. The loop is draft → review → back → review → done."""

    DRAFT = "draft"
    #: With the approvers, waiting.
    PENDING_APPROVAL = "pending_approval"
    #: An approver wants something changed; it is back with the requester.
    CHANGES_REQUESTED = "changes_requested"
    #: Approved. This is also what "in the queue to be created in Zoho" means —
    #: a separate queued state would be a second flag saying the same thing, and
    #: the two would eventually disagree.
    APPROVED = "approved"
    REJECTED = "rejected"
    #: Approved once, and the customer came back. Another round, with the
    #: previous ones kept: what was quoted before is the thing being argued
    #: about, so losing it would lose the argument.
    IN_NEGOTIATION = "in_negotiation"
    #: Reserved for when the Zoho step is built. Nothing sets it yet.
    CREATED_IN_ZOHO = "created_in_zoho"


#: A request in one of these is with the requester, not the approvers.
EDITABLE_STATUSES = frozenset(
    {QuoteStatus.DRAFT, QuoteStatus.CHANGES_REQUESTED, QuoteStatus.IN_NEGOTIATION}
)


class ReviewAction(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    #: Send it back for changes. The request returns to the requester and the
    #: loop goes round again.
    REWORK = "rework"
    #: A remark that decides nothing, so the request stays where it is.
    COMMENT = "comment"
    #: Not a decision at all: the customer came back on an approved quote and it
    #: is open again. Kept in the same history because the history is what
    #: happened to this quote, and this happened.
    NEGOTIATE = "negotiate"


class CommentTarget(StrEnum):
    """What a comment is attached to.

    Anchoring matters: "this rate looks wrong" is useful on the rate and nearly
    useless in a list at the bottom of the page.
    """

    QUOTE = "quote"
    FIELD = "field"
    ITEM = "item"
    SUPPLIER_QUOTE = "supplier_quote"


class QuoteRequest(Base, UUIDPrimaryKey, Timestamped):
    __tablename__ = "quote_requests"
    __table_args__ = (
        Index("ix_quote_requests_team_status", "team_id", "status"),
        CheckConstraint("discount >= 0", name="ck_quote_request_discount"),
    )

    #: Short human reference, unique per team, e.g. "QR-0042".
    reference: Mapped[str | None] = mapped_column(String(60), index=True)

    #: The Proposals list row this was raised from, when it was raised from one.
    #: Kept so the quote and the task that asked for it stay connected — the
    #: reviewer of a negotiation wants the original enquiry, not a retyped
    #: summary of it. SharePoint is the system of record and nothing here is
    #: written back to it.
    source_task_id: Mapped[str | None] = mapped_column(String(120), index=True)
    source_task_url: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(String(200), nullable=False)

    # ── the Zoho Books estimate shape ──────────────────────────────────
    # Named as Zoho names them so the eventual push is a mapping, not a rewrite.
    customer_name: Mapped[str] = mapped_column(String(200), nullable=False)
    #: Zoho's contact id, once we know it. Null until the customer is matched.
    customer_id: Mapped[str | None] = mapped_column(String(60))
    contact_person: Mapped[str | None] = mapped_column(String(200))
    #: The customer's own reference — their PO or enquiry number.
    reference_number: Mapped[str | None] = mapped_column(String(120))
    quote_date: Mapped[date | None] = mapped_column(DateTime(timezone=True))
    expiry_date: Mapped[date | None] = mapped_column(DateTime(timezone=True))
    currency: Mapped[str] = mapped_column(String(3), default="AED", nullable=False)
    salesperson_name: Mapped[str | None] = mapped_column(String(200))
    place_of_supply: Mapped[str | None] = mapped_column(String(120))
    payment_terms: Mapped[str | None] = mapped_column(String(200))
    delivery_terms: Mapped[str | None] = mapped_column(String(200))
    #: Bid closing date. Zoho holds it as the custom field ``cf_bcd`` and the
    #: SharePoint Proposals list calls it BCD — the same date, three names.
    cf_bcd: Mapped[date | None] = mapped_column(DateTime(timezone=True))
    #: ``cf_portal`` in Zoho: which client portal the enquiry arrived through.
    cf_portal: Mapped[str | None] = mapped_column(String(120))

    subject: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    terms: Mapped[str | None] = mapped_column(Text)

    discount: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), default=Decimal(0), server_default=text("0"), nullable=False
    )
    shipping_charge: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), default=Decimal(0), server_default=text("0"), nullable=False
    )
    adjustment: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), default=Decimal(0), server_default=text("0"), nullable=False
    )

    # ── supplier quotes behind it ──────────────────────────────────────
    #: Set when several suppliers quoted the same requirement. Turning it on is
    #: what makes the comparison and the "which one won" decision meaningful.
    multiple_supplier_quotes: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    #: The comparison built from those supplier quotes, reusing the module that
    #: already extracts and compares them rather than a second implementation.
    comparison_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("quote_comparisons.id", ondelete="SET NULL")
    )
    #: Which supplier's offer was chosen. Set by an approver, not the requester —
    #: choosing the supplier is the decision being approved.
    selected_supplier_quote_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("supplier_quotes.id", ondelete="SET NULL")
    )

    #: Chance of winning, and what it was based on. Frozen at save time — the
    #: number is only meaningful next to the history it came from.
    win_probability: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    win_basis: Mapped[dict | None] = mapped_column(JSONB)

    # ── workflow ───────────────────────────────────────────────────────
    status: Mapped[QuoteStatus] = mapped_column(
        String(24), default=QuoteStatus.DRAFT, nullable=False, index=True
    )
    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
    )
    created_by_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Who has to act next. The requester while it is being worked on; unchanged
    #: while it is with approvers, so a reworked request goes back to a person
    #: rather than into a pool nobody owns.
    assigned_to_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: When the approvers were emailed, and what stopped it if they were not.
    #: Recorded rather than logged: "did they get told?" is a question somebody
    #: asks about a specific quote, usually the day it matters.
    approvers_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    notify_error: Mapped[str | None] = mapped_column(Text)
    #: How many times it has been round the review loop. Worth seeing: a quote
    #: on its fourth rework is telling you something a status cannot.
    revision: Mapped[int] = mapped_column(
        Integer, default=1, server_default=text("1"), nullable=False
    )

    team: Mapped[Team] = relationship(lazy="joined")
    created_by: Mapped[User] = relationship(foreign_keys=[created_by_id], lazy="joined")
    assigned_to: Mapped[User | None] = relationship(
        foreign_keys=[assigned_to_id], lazy="joined"
    )
    comparison: Mapped[QuoteComparison | None] = relationship(lazy="selectin")
    revisions: Mapped[list[QuoteRevision]] = relationship(
        back_populates="request",
        cascade="all, delete-orphan",
        order_by="QuoteRevision.revision",
        lazy="selectin",
    )
    items: Mapped[list[QuoteRequestItem]] = relationship(
        back_populates="request",
        cascade="all, delete-orphan",
        order_by="QuoteRequestItem.position",
        lazy="selectin",
    )
    reviews: Mapped[list[QuoteReview]] = relationship(
        back_populates="request",
        cascade="all, delete-orphan",
        order_by="QuoteReview.created_at",
        lazy="selectin",
    )
    comments: Mapped[list[QuoteComment]] = relationship(
        back_populates="request",
        cascade="all, delete-orphan",
        order_by="QuoteComment.created_at",
        lazy="selectin",
    )

    @property
    def is_editable(self) -> bool:
        return self.status in EDITABLE_STATUSES

    @property
    def sub_total(self) -> Decimal:
        return sum((i.line_total for i in self.items), Decimal(0))

    @property
    def total(self) -> Decimal:
        """Computed here, never accepted from a caller — see the comparison
        module for the same rule and the same reason."""
        return self.sub_total - self.discount + self.shipping_charge + self.adjustment

    def __repr__(self) -> str:
        return f"<QuoteRequest {self.reference or self.title!r} {self.status}>"


class QuoteRevision(Base, UUIDPrimaryKey, Timestamped):
    """What a quote was, at the moment a round of it ended.

    The line items are replaced wholesale every time a quote is repriced, which
    is right — a quote is one document, not an accumulation. But it means that
    without this table the previous round is simply gone, and a negotiation is
    an argument about numbers nobody can see any more. So each decision leaves
    the round behind it in full.

    Stored as a snapshot rather than as rows that mirror the live tables: this
    is a record of what was true, and a normalised copy would drift the moment
    anything upstream is renamed. It is read, never joined on.
    """

    __tablename__ = "quote_revisions"
    __table_args__ = (
        UniqueConstraint("request_id", "revision", name="uq_quote_revision_round"),
    )

    request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("quote_requests.id", ondelete="CASCADE"), nullable=False
    )
    #: The round this was. Matches the ``revision`` on the reviews and comments
    #: written during it.
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    #: What ended it — approved, rejected, sent back.
    outcome: Mapped[str] = mapped_column(String(30), nullable=False)
    #: Everything the round was: the form, its lines, its totals, the supplier it
    #: was priced from and the win probability it carried at the time.
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)

    request: Mapped[QuoteRequest] = relationship(back_populates="revisions")

    def __repr__(self) -> str:
        return f"<QuoteRevision r{self.revision} {self.outcome}>"


class QuoteRequestItem(Base, UUIDPrimaryKey, Timestamped):
    """One priced line, named as Zoho names them."""

    __tablename__ = "quote_request_items"
    __table_args__ = (
        CheckConstraint("quantity >= 0", name="ck_quote_item_quantity"),
        Index("ix_quote_request_items_request", "request_id"),
    )

    request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("quote_requests.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    name: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    item_code: Mapped[str | None] = mapped_column(String(120))
    brand: Mapped[str | None] = mapped_column(String(120))
    unit: Mapped[str | None] = mapped_column(String(40))
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal(1), nullable=False)
    #: Zoho calls the unit price ``rate``.
    rate: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal(0), nullable=False)
    discount: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), default=Decimal(0), server_default=text("0"), nullable=False
    )
    tax_name: Mapped[str | None] = mapped_column(String(60))
    tax_percentage: Mapped[Decimal | None] = mapped_column(Numeric(6, 3))

    #: What this line costs the business, when it came from a supplier quote.
    #: Kept so margin is visible while the quote is being reviewed.
    cost_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    #: Which supplier line this was priced from, when it came from one.
    source_supplier_quote_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("supplier_quotes.id", ondelete="SET NULL")
    )

    request: Mapped[QuoteRequest] = relationship(back_populates="items")

    @property
    def line_total(self) -> Decimal:
        return (self.quantity or Decimal(0)) * (self.rate or Decimal(0)) - (
            self.discount or Decimal(0)
        )

    @property
    def margin(self) -> Decimal | None:
        """What we make on this line, if the cost is known."""
        if self.cost_rate is None:
            return None
        return ((self.rate or Decimal(0)) - self.cost_rate) * (self.quantity or Decimal(0))

    def __repr__(self) -> str:
        return f"<QuoteRequestItem {self.name[:30]!r} x{self.quantity}>"


class QuoteReview(Base, UUIDPrimaryKey, Timestamped):
    """One approver's decision. Appended, never edited.

    An approval history that can be rewritten is not a history — the point of
    keeping it is that somebody can ask, months later, who agreed to this.
    """

    __tablename__ = "quote_reviews"
    __table_args__ = (Index("ix_quote_reviews_request", "request_id"),)

    request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("quote_requests.id", ondelete="CASCADE"), nullable=False
    )
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    action: Mapped[ReviewAction] = mapped_column(String(20), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    #: Which revision was being looked at. Without it, a note from two rounds ago
    #: reads as though it were about the quote in front of you.
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    #: The supplier chosen, when the action was an approval.
    selected_supplier_quote_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("supplier_quotes.id", ondelete="SET NULL")
    )

    request: Mapped[QuoteRequest] = relationship(back_populates="reviews")
    reviewer: Mapped[User | None] = relationship(foreign_keys=[reviewer_id], lazy="joined")

    def __repr__(self) -> str:
        return f"<QuoteReview {self.action} r{self.revision}>"


class QuoteComment(Base, UUIDPrimaryKey, Timestamped):
    """A remark anchored to something specific."""

    __tablename__ = "quote_comments"
    __table_args__ = (
        Index("ix_quote_comments_request_target", "request_id", "target_type"),
    )

    request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("quote_requests.id", ondelete="CASCADE"), nullable=False
    )
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    target_type: Mapped[CommentTarget] = mapped_column(
        String(20), default=CommentTarget.QUOTE, nullable=False
    )
    #: Which one. A field name for ``field``, a row id for ``item`` or
    #: ``supplier_quote``, and null for a remark about the whole request.
    target_ref: Mapped[str | None] = mapped_column(String(120))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    #: Which revision it was written against, so an old note is visibly old
    #: rather than looking like a live objection.
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    #: Dealt with. Kept rather than deleted: the exchange is the record of why
    #: the quote ended up as it did.
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    request: Mapped[QuoteRequest] = relationship(back_populates="comments")
    author: Mapped[User | None] = relationship(foreign_keys=[author_id], lazy="joined")

    @property
    def is_open(self) -> bool:
        return self.resolved_at is None

    def __repr__(self) -> str:
        return f"<QuoteComment {self.target_type}:{self.target_ref} {self.body[:24]!r}>"
