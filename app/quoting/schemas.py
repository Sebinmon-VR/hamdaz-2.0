"""Request and response shapes for quote requests.

The field names mirror a Zoho Books estimate on purpose — ``customer_name``,
``reference_number``, ``expiry_date``, items with ``rate`` — so the eventual
integration is a mapping rather than a translation. ``cf_bcd`` and ``cf_portal``
keep Zoho's own custom-field keys.

On top of that sits the *bid pack* — the fields a tender asks for and an
estimate has no room for, plus three lists: the landed-cost build-up, the
compliance matrix and the values to be typed into the buyer's portal. Every
figure derived from them arrives in ``bid``, which is computed on read and
never accepted from a caller. See ``app.quoting.bidpack``.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.quoting import (
    CommentTarget,
    ComplianceArea,
    ComplianceStatus,
    CostStage,
    QuoteStatus,
    ReviewAction,
    Severity,
)
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


class CostLineIn(BaseModel):
    """One row of the landed-cost build-up.

    The goods themselves are *not* one of these — they come from the quote's own
    priced lines, so that repricing from another supplier moves the cost with it.
    These are everything on top: haulage, freight, certificates, clearance.

    Duty and financing are not here either. Both are arithmetic on figures the
    request already holds, and are computed on read.
    """

    stage: CostStage = CostStage.ORIGIN
    label: str = Field(min_length=1, max_length=300)
    #: Where the number came from — "supplier quotation (firm)", "our estimate".
    basis: str | None = Field(default=None, max_length=300)
    #: The figure as quoted, in whatever currency it was quoted in.
    amount_source: Decimal | None = None
    source_currency: str | None = Field(default=None, min_length=3, max_length=3)
    #: The same money in the quote's currency. This is what the totals add up.
    amount_base: Decimal = Decimal(0)
    #: Set on the supplier's own goods price, when it is carried as a row rather
    #: than derived. Normally false — the derived goods row is the principal.
    is_principal: bool = False
    #: The supplier or forwarder has committed to it, rather than us guessing.
    is_firm: bool = False
    notes: str | None = None


class CostLineOut(CostLineIn):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    position: int


class ComplianceIn(BaseModel):
    """One RFP requirement against what the supplier actually offered."""

    #: The row's own id, sent back on a save. The list is replaced wholesale
    #: like the line items, and without this the moment a gap was closed would
    #: be rewritten to "now" every time anybody saved anything.
    id: uuid.UUID | None = None
    #: The matrix's short handle — "T4", "C11". What people call it.
    ref: str | None = Field(default=None, max_length=20)
    area: ComplianceArea = ComplianceArea.COMMERCIAL
    requirement: str = Field(min_length=1)
    #: Which clause says so.
    source_clause: str | None = Field(default=None, max_length=120)
    supplier_position: str | None = None
    status: ComplianceStatus = ComplianceStatus.OPEN
    #: Set to put the row on the red-flag list, worst first.
    severity: Severity | None = None
    action: str | None = None
    owner: str | None = Field(default=None, max_length=200)
    #: Whether the gap has been closed. A date is kept, but the caller only says
    #: yes or no — the moment it was ticked is the server's to record.
    resolved: bool = False


class ComplianceOut(ComplianceIn):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    position: int
    resolved_at: datetime | None
    is_open: bool
    #: Unresolved and in a state that ought to stop a submission.
    is_blocking: bool


class SubmissionFieldIn(BaseModel):
    """One value to be typed into the buyer's portal, and where it goes."""

    #: Sent back on a save, so that when the cell was actually typed survives
    #: the replace. See ``ComplianceIn.id``.
    id: uuid.UUID | None = None
    clause: str | None = Field(default=None, max_length=40)
    label: str = Field(min_length=1, max_length=300)
    #: A cell reference, a tab and cell, or a field name — every portal names
    #: its own places, so this is free text.
    destination: str | None = Field(default=None, max_length=120)
    value: str | None = None
    note: str | None = None
    is_mandatory: bool = False
    #: Ticked off by whoever did the typing.
    entered: bool = False


class SubmissionFieldOut(SubmissionFieldIn):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    position: int
    entered_at: datetime | None


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

    # ── the bid pack ───────────────────────────────────────────────────
    # All optional. A quote for somebody who rang up and asked for a price fills
    # in none of it and should not be asked to.
    rfp_number: str | None = Field(default=None, max_length=120)
    buying_entity: str | None = Field(default=None, max_length=200)
    #: The RFP's own numbering for the line being bid — "3.13.5 — CLOTH".
    line_item_ref: str | None = Field(default=None, max_length=120)
    manufacturer_name: str | None = Field(default=None, max_length=200)
    manufacturer_part_number: str | None = Field(default=None, max_length=120)
    manufacturer_class_no: str | None = Field(default=None, max_length=120)
    #: The Incoterm the RFP demands, and the place it names.
    incoterm_required: str | None = Field(default=None, max_length=40)
    incoterm_place: str | None = Field(default=None, max_length=200)
    ship_to: str | None = Field(default=None, max_length=200)
    requested_delivery_date: date | None = None
    #: What we will actually commit to, in calendar days from the PO.
    delivery_days: int | None = Field(default=None, ge=0, le=3650)
    #: ISO 3166 alpha-2, upper-cased. Portals want the code, and defaulting it
    #: to our own country on a specified-brand line is the classic desk error.
    country_of_origin: str | None = Field(default=None, min_length=2, max_length=2)
    mode_of_shipment: str | None = Field(default=None, max_length=60)
    bid_validity_days: int | None = Field(default=None, ge=0, le=3650)
    bid_reference: str | None = Field(default=None, max_length=120)
    technical_verdict: str | None = None
    commercial_verdict: str | None = None

    # The landed-cost inputs. Inputs only — every total is computed on read.
    supplier_currency: str | None = Field(default=None, min_length=3, max_length=3)
    #: One unit of ``currency`` in ``supplier_currency`` — "1 USD = 3.672501
    #: AED" — as Zoho Books states it, which is the rate the estimate will be
    #: converted at.
    fx_rate: Decimal | None = Field(default=None, gt=0)
    customs_duty_percent: Decimal = Field(default=Decimal(0), ge=0, le=100)
    financing_rate_percent: Decimal = Field(default=Decimal(0), ge=0, le=100)
    cash_exposure_days: int = Field(default=0, ge=0, le=3650)
    #: The margin the bid as a whole is built at, over landed cost.
    target_markup_percent: Decimal | None = Field(default=None, ge=0, le=1000)
    #: The rounded price somebody decided on. Held rather than recomputed —
    #: rounding a bid up to a clean figure is a decision, not an accident.
    submission_unit_price: Decimal | None = Field(default=None, ge=0)
    submission_total: Decimal | None = Field(default=None, ge=0)
    #: The RFP makes the principal's own quotation a mandatory attachment, so
    #: the buyer will see what we paid.
    discloses_principal_price: bool = False

    #: Turn on when several suppliers quoted the same requirement. The comparison
    #: and the "which supplier won" decision only mean anything when it is set.
    multiple_supplier_quotes: bool = False
    items: list[ItemIn] = Field(default_factory=list)
    #: The build-up on top of the goods. Replaced wholesale, like the items.
    cost_lines: list[CostLineIn] = Field(default_factory=list)
    compliance: list[ComplianceIn] = Field(default_factory=list)
    submission_fields: list[SubmissionFieldIn] = Field(default_factory=list)

    @field_validator("country_of_origin", "currency", "supplier_currency")
    @classmethod
    def _upper(cls, value: str | None) -> str | None:
        """Codes are codes. "gb" and "GB" are the same country and a portal that
        is handed the first will reject it."""
        return value.upper() if value else value


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


class CostElementOut(BaseModel):
    """One row of the build-up as it is read, typed rows and derived rows alike.

    ``computed`` is shown rather than hidden: somebody checking a landed cost
    needs to know which figures they can argue with and which ones follow from
    the figures above them.
    """

    ref: int
    stage: CostStage
    label: str
    basis: str | None
    amount_source: Decimal | None
    source_currency: str | None
    amount_base: Decimal
    is_principal: bool
    is_firm: bool
    computed: bool
    notes: str | None
    #: Null on the derived goods, duty and financing rows — there is nothing
    #: to edit on a row that is arithmetic.
    id: str | None = None


class LandedCostOut(BaseModel):
    currency: str
    elements: list[CostElementOut]
    #: Everything up to arrival. What duty is charged on.
    cif_subtotal: Decimal
    customs_duty: Decimal
    financing_cost: Decimal
    destination_subtotal: Decimal
    total: Decimal
    quantity: Decimal | None
    per_unit: Decimal | None
    #: Why there is no per-unit figure, when there is not.
    per_unit_note: str | None
    #: The share the supplier or forwarder has committed to. The rest is our
    #: estimate, and every point of it that comes in high costs us margin.
    firm_percent: Decimal
    principal_value: Decimal


class MarkupScenarioOut(BaseModel):
    markup_percent: Decimal
    unit_sell: Decimal | None
    total_sell: Decimal
    #: Margin as a share of the selling price — which is what "margin" means to
    #: everybody except the person who applied the markup.
    margin_percent: Decimal
    is_target: bool


class DisclosureOut(BaseModel):
    """What the buyer will make of our price if they are shown the supplier's."""

    principal_value: Decimal
    bid_value: Decimal
    apparent_uplift_percent: Decimal | None
    #: The part of the uplift that is genuine landed cost rather than margin.
    recoverable_cost: Decimal
    true_margin_percent: Decimal | None
    disclosed: bool


class RedFlagOut(BaseModel):
    id: str
    ref: str | None
    severity: Severity
    status: str
    issue: str
    action: str | None
    owner: str | None
    resolved: bool


class BidPackOut(BaseModel):
    """Everything derived from the bid inputs. Computed on read, never stored.

    A saved total and the inputs it came from disagree the first time anybody
    edits one, and the one people believe is always the wrong one. So there is
    no saved total — only the parts, and ``app.quoting.bidpack``.
    """

    landed: LandedCostOut
    scenarios: list[MarkupScenarioOut]
    #: The rung the bid is actually built at, when a markup is set.
    target: MarkupScenarioOut | None
    bid_unit_price: Decimal | None
    bid_total: Decimal
    #: True when the total is the ladder's answer rather than somebody's
    #: decision, so a screen can say which it is showing.
    bid_total_is_suggested: bool
    gross_margin: Decimal
    gross_margin_percent: Decimal | None
    disclosure: DisclosureOut
    #: The compliance rows carrying a severity, worst first.
    red_flags: list[RedFlagOut]
    #: What somebody should be told before this goes anywhere. Advisory: it is
    #: said plainly and it takes no button away.
    warnings: list[str]


class QuoteDocumentOut(BaseModel):
    """One supplier document that was uploaded against this quote.

    Read off the saved supplier-quote rows rather than out of the comparison
    analysis. The analysis is a snapshot of a computation, made from the payload
    before anything is filed anywhere — so where a document ended up is simply
    not known at the point it is built. The rows are, and stay, the truth about
    the files.
    """

    supplier_quote_id: uuid.UUID
    supplier_name: str
    file_name: str | None
    file_type: str | None
    #: Where it was filed in the shared library, when filing is on and worked.
    #: Opening it uses the viewer's own SharePoint access, never this app's.
    drive_url: str | None
    #: True for the offer this quote is actually priced from.
    is_selected: bool = False


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
    #: Before tax, and the tax on its own — a customer reads all three numbers.
    total_excl_tax: Decimal
    tax_total: Decimal
    #: Computed here, never accepted from the caller. Tax included.
    total: Decimal

    # ── the bid pack, as stored ────────────────────────────────────────
    rfp_number: str | None = None
    buying_entity: str | None = None
    line_item_ref: str | None = None
    manufacturer_name: str | None = None
    manufacturer_part_number: str | None = None
    manufacturer_class_no: str | None = None
    incoterm_required: str | None = None
    incoterm_place: str | None = None
    ship_to: str | None = None
    requested_delivery_date: datetime | None = None
    delivery_days: int | None = None
    country_of_origin: str | None = None
    mode_of_shipment: str | None = None
    bid_validity_days: int | None = None
    bid_reference: str | None = None
    technical_verdict: str | None = None
    commercial_verdict: str | None = None
    supplier_currency: str | None = None
    fx_rate: Decimal | None = None
    customs_duty_percent: Decimal = Decimal(0)
    financing_rate_percent: Decimal = Decimal(0)
    cash_exposure_days: int = 0
    target_markup_percent: Decimal | None = None
    submission_unit_price: Decimal | None = None
    submission_total: Decimal | None = None
    discloses_principal_price: bool = False

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
    #: The build-up on top of the goods. The goods themselves are not here —
    #: they come from the priced lines, and appear in ``bid.landed.elements``.
    cost_lines: list[CostLineOut] = Field(default_factory=list)
    compliance: list[ComplianceOut] = Field(default_factory=list)
    submission_fields: list[SubmissionFieldOut] = Field(default_factory=list)
    #: Everything derived: the landed cost, the margin ladder, the price the
    #: buyer will read against the principal's, and what is still outstanding.
    #: Filled in by the router from ``app.quoting.bidpack``.
    bid: BidPackOut | None = None
    #: Every round that has ended, oldest first. What a negotiation is argued
    #: over: the prices that were quoted before, beside the ones being quoted
    #: now, with the win probability each round carried.
    revisions: list[RevisionOut] = Field(default_factory=list)

    #: The comparison of the supplier quotes behind this one, when there is one.
    comparison: dict[str, Any] | None = None
    #: The documents people uploaded, and where each was filed. Kept apart from
    #: ``comparison`` because that is a computation and these are files.
    documents: list[QuoteDocumentOut] = Field(default_factory=list)
    #: Every sum on the quote, written out in the order it runs. See
    #: ``app.quoting.calculation``.
    calculation: list[CalcStepOut] = Field(default_factory=list)
    #: Whether the caller may edit, may send it for approval, and may decide.
    may_edit: bool = False
    #: Whether the caller may set the currency — which, unlike everything else,
    #: is not frozen by submitting. It is a label on figures that are already
    #: what they are, and nothing here converts, so it stays correctable for as
    #: long as the quote is the caller's. Told apart from ``may_edit`` because
    #: that one answers about the whole document and goes false the moment a
    #: quote goes up.
    may_set_currency: bool = False
    #: Set once it is priced from a supplier and has lines. ``submit_reason``
    #: says what is missing while it is not, so a form can say why the button is
    #: off instead of only finding out when it is pressed.
    may_submit: bool = False
    submit_reason: str | None = None
    may_approve: bool = False
    approve_reason: str | None = None
    #: Super admin only. A quote carries an approval history that is appended
    #: and never edited, so removing one is not the author's to do.
    may_delete: bool = False

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
    #: Unresolved compliance rows that ought to stop a submission. On a list of
    #: bids this is the column people scan — a total tells you what a bid is
    #: worth, this tells you whether it can be sent.
    blocking_issues: int = 0
    #: The buyer's own event number, when there is one.
    rfp_number: str | None = None
    #: The deadline the work is actually timed against.
    cf_bcd: datetime | None = None
    #: Super admin only, and the server's answer rather than a role check done
    #: on the screen. Identical for every row of a given caller, but carried per
    #: row so the list asks exactly the question the quote itself does.
    may_delete: bool = False
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


class FxQuoteOut(BaseModel):
    """Zoho Books' rate between two currencies, with its working."""

    model_config = ConfigDict(from_attributes=True)

    quote_currency: str
    supplier_currency: str
    #: One unit of the quote's currency in the supplier's — "1 USD = 3.672501
    #: AED" is 3.672501 — the shape of ``fx_rate``, written in unchanged.
    rate: Decimal
    base_currency: str
    quote_in_base: Decimal
    supplier_in_base: Decimal
    effective_date: date | None
    source: str


class CalcStepOut(BaseModel):
    """One line of the working behind a quote's figures."""

    model_config = ConfigDict(from_attributes=True)

    group: str
    label: str
    working: str
    result: Decimal
    currency: str | None


class CurrencyIn(BaseModel):
    """The currency a quote is stated in.

    Its own body, and its own route, because it is the one field allowed to move
    on a quote that is otherwise frozen — and because switching it converts
    every figure at Zoho's rate, which is not a field edit.
    """

    currency: str = Field(min_length=3, max_length=3)

    @field_validator("currency")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()


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
