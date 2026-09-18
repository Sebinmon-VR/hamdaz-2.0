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

Seven tables, because seven different things change at different times:

* ``quote_requests`` — the form and where it is in the workflow.
* ``quote_request_items`` — the priced lines, which is what a quote *is*.
* ``quote_reviews`` — one approver's decision, appended and never edited. An
  approval history that can be rewritten is not a history.
* ``quote_comments`` — remarks anchored to a particular field, line or supplier
  quote, so "this rate looks wrong" points at the rate rather than floating at
  the bottom of the page.
* ``quote_cost_lines`` — the landed-cost build-up behind the price. What it
  costs to put the supplier's goods on the customer's floor.
* ``quote_compliance_items`` — one RFP requirement against what the supplier
  actually offered, and what has to be done about the gap.
* ``quote_submission_fields`` — the values to type into the buyer's own portal.

**The bid pack.** A Zoho estimate is one number against a customer name, and
that is all a quote needs when somebody asks us for a price. A *tender* is not
that. A tender arrives as an RFP with numbered clauses, a mandatory technical
specification, a portal with named cells to fill, and a price that has to be
justified line by line because the buyer will see the principal's own quotation
next to ours. The three tables above plus the ``bid`` fields on the request are
what turn a quote into a bid: the compliance position, the cost build-up and the
portal answers, held beside the estimate rather than in a spreadsheet somebody
mails around.

Every derived figure — the CIF subtotal, the duty, the landed cost, the margin
ladder, the uplift the buyer will see — is computed from the stored inputs and
never stored itself. A saved total and the inputs it came from disagree the
first time anybody edits one, and the one people trust is always the wrong one.
See ``app/quoting/bidpack.py``, which does the arithmetic in one place.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
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


class ComplianceStatus(StrEnum):
    """Where one RFP requirement stands against what the supplier offered.

    The buyer's own matrices use a traffic light, and so does this, but three
    colours cannot say the two things that matter most: whether a gap can be
    *priced* and whether it can be *cured*. So a deviation — priceable, and we
    carry the cost — is a different status from non-compliance, which has to be
    cured or formally declared before anything is submitted.
    """

    #: Offered exactly what was asked for.
    COMPLIANT = "compliant"
    #: A gap, but one we can price or cure. Amber.
    DEVIATION = "deviation"
    #: A gap that must be cured or declared before submission. Red.
    NON_COMPLIANT = "non_compliant"
    #: The RFP contradicts itself, or does not say. Ask the buyer.
    CLARIFY = "clarify"
    #: Compliant on its face and still dangerous — price disclosure, say.
    RISK = "risk"
    #: Ours to produce and not produced yet.
    OPEN = "open"
    #: Genuinely does not apply. Kept rather than deleted so the matrix still
    #: answers "did anyone look at clause 3.7", which a missing row cannot.
    NOT_APPLICABLE = "not_applicable"
    #: Noted for the file — a packing spec, a tariff code. Decides nothing.
    NOTED = "noted"


#: Statuses that stop a submission until somebody deals with them.
BLOCKING_COMPLIANCE = frozenset(
    {ComplianceStatus.NON_COMPLIANT, ComplianceStatus.CLARIFY, ComplianceStatus.OPEN}
)


class Severity(StrEnum):
    """How badly a compliance row wants attention before submission.

    Separate from the status because they answer different questions. A row can
    be a *deviation* (priceable, amber) and still be the single thing most
    likely to lose the bid. The red-flag list on a bid summary is exactly the
    rows that carry one of these, worst first — derived from the matrix rather
    than typed a second time beside it, because two lists of the same problems
    diverge the first week and then nobody knows which one is current.
    """

    #: Submission fails outright until this is fixed.
    STOPPER = "stopper"
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    #: Worth saying, blocks nothing.
    NOTE = "note"


#: Worst first. The order the red flags are read in.
SEVERITY_ORDER: dict[str, int] = {
    Severity.STOPPER: 0,
    Severity.CRITICAL: 1,
    Severity.HIGH: 2,
    Severity.MEDIUM: 3,
    Severity.NOTE: 4,
}


class ComplianceArea(StrEnum):
    """Which part of the bid a requirement belongs to — the matrix's sections."""

    TECHNICAL = "technical"
    COMMERCIAL = "commercial"
    #: The bid package itself: certificates, statements, the power of attorney.
    DOCUMENTS = "documents"
    LOGISTICS = "logistics"


class CostStage(StrEnum):
    """Which side of the customs border a cost element sits on.

    Not decoration: duty is charged on the CIF value, so what counts towards
    that value and what lands after it is the difference between the right duty
    and a number somebody made up.
    """

    #: Everything up to and including arrival — goods, origin handling,
    #: freight, insurance. Sums to the CIF value that duty is charged on.
    ORIGIN = "origin"
    #: Clearance, inland delivery, bank charges. After the duty base.
    DESTINATION = "destination"


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

    # ── the bid pack ───────────────────────────────────────────────────
    # A tender is not an estimate with a longer subject line. These are what an
    # RFP asks for and a Zoho estimate has no room for. All nullable: a quote
    # for a customer who rang up and asked for a price fills in none of them,
    # and should not be nagged about it.

    #: The buyer's event number — "RFP 6000149233". Theirs, not ours; every
    #: clarification, every portal message and every eventual PO quotes it.
    rfp_number: Mapped[str | None] = mapped_column(String(120), index=True)
    #: Who is actually buying, which is often not who ran the tender — an
    #: operating company inside the group the portal belongs to.
    buying_entity: Mapped[str | None] = mapped_column(String(200))
    #: The RFP's own numbering for the line being bid, e.g. "3.13.5 — CLOTH".
    #: Answers to the buyer are indexed by this and nothing else.
    line_item_ref: Mapped[str | None] = mapped_column(String(120))

    # The manufacturer, as the RFP names it. A specified-brand line is won or
    # lost on these three matching character for character, so they are held
    # apart from the line items rather than buried in a description.
    manufacturer_name: Mapped[str | None] = mapped_column(String(200))
    manufacturer_part_number: Mapped[str | None] = mapped_column(String(120))
    #: The buyer's own material/class number for the item.
    manufacturer_class_no: Mapped[str | None] = mapped_column(String(120))

    #: The Incoterm the RFP demands — DAP, DDP, CIF — and the place it names.
    #: Held apart from ``delivery_terms``, which is prose: the gap between the
    #: demanded term and the supplier's own is the single largest cost on most
    #: bids, and comparing two sentences will not find it.
    incoterm_required: Mapped[str | None] = mapped_column(String(40))
    incoterm_place: Mapped[str | None] = mapped_column(String(200))
    #: Where the goods are actually to be delivered, as the RFP states it. Not
    #: always the Incoterm place, and when the two disagree somebody has to ask.
    ship_to: Mapped[str | None] = mapped_column(String(200))
    #: The date the buyer asked for. Frequently already past by the time the
    #: enquiry reaches us, which is itself a deviation to declare.
    requested_delivery_date: Mapped[date | None] = mapped_column(DateTime(timezone=True))
    #: What we will actually commit to, in calendar days from the PO. The
    #: number that goes in the portal.
    delivery_days: Mapped[int | None] = mapped_column(Integer)
    #: ISO 3166 alpha-2, because portals want the code. Defaulting it to the
    #: bidder's own country is the most common and most expensive desk error on
    #: a specified-brand line — the goods are made wherever the OEM makes them.
    country_of_origin: Mapped[str | None] = mapped_column(String(2))
    #: Air, sea, road — as the portal's own list spells it.
    mode_of_shipment: Mapped[str | None] = mapped_column(String(60))
    #: How long our price stands, in days. The RFP sets it; the supplier's own
    #: validity is usually shorter, and bidding the long one against the short
    #: one is an unhedged position rather than a rounding difference.
    bid_validity_days: Mapped[int | None] = mapped_column(Integer)
    #: Our own quotation reference as given to the buyer.
    bid_reference: Mapped[str | None] = mapped_column(String(120))

    #: Where the bid stands technically and commercially, in a sentence each.
    #: Two verdicts rather than one: an offer can be the OEM's own product
    #: against a specification copied from their catalogue — unimprovable —
    #: and still be commercially unbiddable on payment terms.
    technical_verdict: Mapped[str | None] = mapped_column(Text)
    commercial_verdict: Mapped[str | None] = mapped_column(Text)

    # ── the landed-cost inputs ─────────────────────────────────────────
    # Inputs only. Every total derived from them lives in ``app.quoting.bidpack``
    # and is computed on read.

    #: The currency the supplier quoted in, when it is not ours.
    supplier_currency: Mapped[str | None] = mapped_column(String(3))
    #: Units of :attr:`currency` per unit of :attr:`supplier_currency`, at the
    #: rate the bid is costed on — mid-market plus a spread, not the mid-market
    #: rate. The bid stands for months and the money moves once, at the end.
    fx_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    #: Import duty, as a percentage of the CIF value.
    customs_duty_percent: Mapped[Decimal] = mapped_column(
        Numeric(6, 3), default=Decimal(0), server_default=text("0"), nullable=False
    )
    #: Cost of the money, per annum, while it is out of the door. Real whenever
    #: a supplier wants paying before the customer pays us.
    financing_rate_percent: Mapped[Decimal] = mapped_column(
        Numeric(6, 3), default=Decimal(0), server_default=text("0"), nullable=False
    )
    #: How many days it is out for — payment to the supplier until settlement.
    cash_exposure_days: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    #: The margin the bid is built at, over landed cost. Distinct from the
    #: markup used to price individual lines from a supplier quote: that one
    #: sets rates, this one is the business's position on the bid as a whole.
    target_markup_percent: Mapped[Decimal | None] = mapped_column(Numeric(7, 3))
    #: The price actually going in, per unit — the rounded, human number. Held
    #: rather than computed because rounding a bid up to a clean figure is a
    #: decision somebody makes, and a recomputed price would quietly undo it.
    submission_unit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    #: The whole bid, when it is not simply unit price times quantity.
    submission_total: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    #: Whether the RFP makes us attach the principal's own quotation. When it
    #: does, the buyer sees what we paid, and the uplift needs the cost
    #: breakdown beside it or it reads as pure margin.
    discloses_principal_price: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
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
    cost_lines: Mapped[list[QuoteCostLine]] = relationship(
        back_populates="request",
        cascade="all, delete-orphan",
        order_by="QuoteCostLine.position",
        lazy="selectin",
    )
    compliance: Mapped[list[QuoteComplianceItem]] = relationship(
        back_populates="request",
        cascade="all, delete-orphan",
        order_by="QuoteComplianceItem.position",
        lazy="selectin",
    )
    submission_fields: Mapped[list[QuoteSubmissionField]] = relationship(
        back_populates="request",
        cascade="all, delete-orphan",
        order_by="QuoteSubmissionField.position",
        lazy="selectin",
    )

    @property
    def is_editable(self) -> bool:
        return self.status in EDITABLE_STATUSES

    @property
    def sub_total(self) -> Decimal:
        return sum((i.line_total for i in self.items), Decimal(0))

    @property
    def total_excl_tax(self) -> Decimal:
        """The figure before tax: lines, less the discount, plus shipping and the
        adjustment. Shown on its own because a customer reads both numbers."""
        return self.sub_total - self.discount + self.shipping_charge + self.adjustment

    @property
    def tax_total(self) -> Decimal:
        """Tax across the lines, each at its own rate, on the line's own total.
        Rounded once, at the end, to the cent."""
        raw = sum(
            (
                i.line_total * (i.tax_percentage or Decimal(0)) / Decimal(100)
                for i in self.items
            ),
            Decimal(0),
        )
        return raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @property
    def total(self) -> Decimal:
        """Computed here, never accepted from a caller — see the comparison
        module for the same rule and the same reason. Tax included: this is the
        number the customer pays."""
        return self.total_excl_tax + self.tax_total

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


class QuoteCostLine(Base, UUIDPrimaryKey, Timestamped):
    """One element of what it costs to land the goods.

    The build-up behind a bid price: the goods themselves, then haulage,
    freight, certificates, insurance, duty, clearance, the bank's cut. On a
    tender this is not bookkeeping — clause after clause makes the price
    breakdown a mandatory attachment, because the buyer wants to see that a
    price at twice the principal's is mostly freight and duty rather than greed.

    Amounts are held twice on purpose. ``amount_source`` is the figure as it was
    quoted or estimated, in whatever currency that was; ``amount_base`` is the
    same money in the quote's currency. Both are stored rather than one being
    derived, because a supplier's GBP figure is *firm* and its converted value
    is only as firm as the rate — and when the rate is revised the record should
    still show what was actually quoted.
    """

    __tablename__ = "quote_cost_lines"
    __table_args__ = (Index("ix_quote_cost_lines_request", "request_id", "position"),)

    request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("quote_requests.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    #: Which side of the duty base this falls on. See :class:`CostStage`.
    stage: Mapped[CostStage] = mapped_column(
        String(20), default=CostStage.ORIGIN, nullable=False
    )
    label: Mapped[str] = mapped_column(String(300), nullable=False)
    #: Where the number came from: "supplier quotation (firm)", "our estimate",
    #: "forwarder's rate". A landed cost is half estimates, and which half is
    #: the first thing an approver asks.
    basis: Mapped[str | None] = mapped_column(String(300))

    #: The figure as quoted, in ``source_currency``. Null when it was only ever
    #: reckoned in the quote's own currency.
    amount_source: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    source_currency: Mapped[str | None] = mapped_column(String(3))
    #: The same money in the quote's currency. This is what the totals add up.
    amount_base: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), default=Decimal(0), server_default=text("0"), nullable=False
    )

    #: Set on the supplier's own quoted goods price — the figure the buyer will
    #: see if the principal's quotation has to be attached. Exactly one line
    #: should carry it; it is what the disclosure exposure is measured against.
    is_principal: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    #: True when the supplier has committed to it, false when we guessed. An
    #: estimate that turns out low comes out of the margin, so the distinction
    #: belongs on the row rather than in somebody's memory.
    is_firm: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    notes: Mapped[str | None] = mapped_column(Text)

    request: Mapped[QuoteRequest] = relationship(back_populates="cost_lines")

    def __repr__(self) -> str:
        return f"<QuoteCostLine {self.label[:30]!r} {self.amount_base}>"


class QuoteComplianceItem(Base, UUIDPrimaryKey, Timestamped):
    """One RFP requirement, what the supplier offered against it, and the gap.

    A row per clause, not a summary paragraph. The reason is that every gap has
    an owner and a deadline, and a paragraph has neither: "obtain a 90-day
    validity confirmation" is work somebody has to do before a date, and on the
    bids that go wrong it is almost always this that was known and unassigned
    rather than unknown.

    ``status`` is the position; ``severity``, when set, is what puts the row on
    the red-flag list. Both, because they are different questions — see
    :class:`Severity`.
    """

    __tablename__ = "quote_compliance_items"
    __table_args__ = (
        Index("ix_quote_compliance_request", "request_id", "position"),
        Index("ix_quote_compliance_severity", "request_id", "severity"),
    )

    request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("quote_requests.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: The matrix's own short handle for the row — "T4", "C11". What people say
    #: to each other about it, and what a comment on the bid will quote.
    ref: Mapped[str | None] = mapped_column(String(20))
    area: Mapped[ComplianceArea] = mapped_column(
        String(20), default=ComplianceArea.COMMERCIAL, nullable=False
    )
    #: What the RFP asks for, in its own words where possible.
    requirement: Mapped[str] = mapped_column(Text, nullable=False)
    #: Which clause of the RFP says so. Without it a disputed row turns into a
    #: search through a hundred-page document while the deadline runs.
    source_clause: Mapped[str | None] = mapped_column(String(120))
    #: What the supplier's offer actually says about it.
    supplier_position: Mapped[str | None] = mapped_column(Text)
    status: Mapped[ComplianceStatus] = mapped_column(
        String(20), default=ComplianceStatus.OPEN, nullable=False
    )
    #: Null unless the row belongs on the red-flag list.
    severity: Mapped[Severity | None] = mapped_column(String(12))
    #: What has to happen, concretely. "Ask Denice for a 90-day price hold",
    #: not "address validity".
    action: Mapped[str | None] = mapped_column(Text)
    #: Who is doing it. A name, free text — the people on a bid are not all
    #: users of this system, and refusing to record a supplier contact as an
    #: owner would push the list back into a spreadsheet.
    owner: Mapped[str | None] = mapped_column(String(200))
    #: When it stopped being a gap. Kept rather than deleted, because what was
    #: cured and when is exactly what a post-mortem on a lost bid asks.
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    request: Mapped[QuoteRequest] = relationship(back_populates="compliance")

    @property
    def is_open(self) -> bool:
        return self.resolved_at is None

    @property
    def is_blocking(self) -> bool:
        """Unresolved, and in a state that should stop a submission."""
        return self.is_open and (
            self.status in BLOCKING_COMPLIANCE or self.severity == Severity.STOPPER
        )

    def __repr__(self) -> str:
        return f"<QuoteComplianceItem {self.ref} {self.status}>"


class QuoteSubmissionField(Base, UUIDPrimaryKey, Timestamped):
    """One value to be typed into the buyer's portal, and where it goes.

    Bids are not submitted from here — they are submitted in the buyer's own
    system, usually by filling in a downloaded workbook and uploading it back.
    That last step is where bids are lost: a mandatory cell left at its default,
    an "intend to respond" flag still reading *No*, a country of origin that
    stayed on the bidder's own country. The values are decided here, so the map
    from decision to cell belongs here too, and the person doing the typing
    works from a checklist rather than from memory.
    """

    __tablename__ = "quote_submission_fields"
    __table_args__ = (Index("ix_quote_submission_request", "request_id", "position"),)

    request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("quote_requests.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: The RFP clause the field answers — "3.12.1".
    clause: Mapped[str | None] = mapped_column(String(40))
    label: Mapped[str] = mapped_column(String(300), nullable=False)
    #: Where it goes in the buyer's workbook — a cell reference, a tab and cell,
    #: or a field name. Free text: every portal names its own places.
    destination: Mapped[str | None] = mapped_column(String(120))
    value: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    #: The portal will not accept the bid without it.
    is_mandatory: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    #: Ticked off by whoever did the typing.
    entered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    request: Mapped[QuoteRequest] = relationship(back_populates="submission_fields")

    def __repr__(self) -> str:
        return f"<QuoteSubmissionField {self.clause} {self.label[:24]!r}>"
