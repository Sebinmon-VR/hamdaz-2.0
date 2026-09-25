"""The selling & costing report: what an approver reads before deciding.

One page, in the order the questions get asked: what are we charging, what
does it cost us landed, what does that leave, and how far can we go if the
customer pushes back. Every figure on it is worked out here from what the
quote already stores — the priced lines, the landed-cost build-up, the tax on
each line — and nothing on it is stored. The PDF (``report_pdf.py``), the
screen and the approval mail all read this one object, so they cannot
disagree with each other or with the quote.

**Margin here is a share of the selling price.** A line bought at 637 and
sold at 2,125.70 makes 64.8% — the margin a salesperson quotes and the one a
discount eats into — and not the 165% markup on cost. The whole quote is
priced in these terms (selling price = cost ÷ (1 − margin)), and so are
``walk_away_margin_percent`` and ``comfortable_margin_percent``.

**Two currencies.** The quote is priced in one currency and the business
reads money in another, so every figure is shown in both when a rate is
known: "USD 3,988.94 / AED 14,649.38 (1 USD = 3.6725 AED)". The rate is the
caller's to supply — the quote's own supplier rate when the supplier is in
AED, Zoho's otherwise — and the report says which it used. Without one, the
report is in the quote's currency alone and says so.

**Per-line landed cost is allocated in proportion to what each line cost.**
Freight, insurance and duty are paid on the shipment, not on a line, so a
line that is half the goods carries half the freight. Lines with no supplier
cost behind them carry nothing and say so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from app.models.quoting import QuoteRequest
from app.quoting import bidpack
from app.quoting.service import supplier_prices

#: The currency the business reads money in. Zoho's base, and the second
#: column of every figure on the report.
BASE_CURRENCY: Final = "AED"

#: House defaults, as a share of the selling price, when a quote sets none.
DEFAULT_WALK_AWAY: Final = Decimal(25)
DEFAULT_COMFORTABLE: Final = Decimal(40)

#: The discounts a customer actually asks for. The ladder is read top to
#: bottom until the status turns.
DISCOUNT_LADDER: Final = (Decimal(10), Decimal(20), Decimal(30), Decimal(40), Decimal(50))

_MONEY: Final = Decimal("0.01")
_PCT: Final = Decimal("0.01")
_ZERO: Final = Decimal(0)

COMFORTABLE: Final = "comfortable"
ACCEPTABLE: Final = "acceptable"
NEEDS_APPROVAL: Final = "needs_approval"
#: The discount leaves less than the landed cost: money out for every unit.
LOSS: Final = "loss"


def _money(value: Decimal | None) -> Decimal:
    return (value or _ZERO).quantize(_MONEY, rounding=ROUND_HALF_UP)


def _pct(value: Decimal | None) -> Decimal | None:
    return None if value is None else value.quantize(_PCT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class Figure:
    """One amount, in the quote's currency and — when a rate is known — in the
    base currency beside it."""

    amount: Decimal
    base: Decimal | None


@dataclass(frozen=True, slots=True)
class ReportLine:
    position: int
    part_number: str | None
    description: str
    quantity: Decimal
    unit: str | None
    #: What the supplier charges for the line, on their own document and in
    #: their own currency where that is known; otherwise our recorded cost.
    supplier_amount: Decimal | None
    supplier_currency: str | None
    landed: Figure | None
    selling: Figure
    margin: Figure | None
    #: Of the selling price.
    margin_percent: Decimal | None


@dataclass(frozen=True, slots=True)
class CostRow:
    label: str
    amount: Figure
    #: Our estimate rather than a committed figure. Starred on the report.
    is_estimate: bool
    #: Worked out from other rows — duty, financing, a rated row.
    computed: bool


@dataclass(frozen=True, slots=True)
class WalkAwayRung:
    margin_percent: Decimal
    price: Figure
    #: How far below the quoted price that is.
    max_discount_percent: Decimal | None


@dataclass(frozen=True, slots=True)
class NegotiationStep:
    discount_percent: Decimal
    total_incl_tax: Figure
    selling: Figure
    margin: Figure
    margin_percent: Decimal | None
    status: str


@dataclass(frozen=True, slots=True)
class CustomerBlock:
    name: str
    end_user: str | None
    reference: str | None
    portal: str | None
    place_of_supply: str | None
    valid_from: date | None
    valid_until: date | None


@dataclass(frozen=True, slots=True)
class SupplierBlock:
    name: str | None
    basis: str | None
    route: str | None
    currency: str | None
    quote_number: str | None
    creator: str | None


@dataclass(frozen=True, slots=True)
class CostingReport:
    reference: str
    title: str
    prepared_on: date
    prepared_by: str | None
    #: Who last decided on it, and who approved it — from the review trail.
    #: Blank until somebody has.
    reviewed_by: str | None
    approved_by: str | None
    currency: str
    base_currency: str
    #: One unit of ``currency`` in ``base_currency``, or None for a single-
    #: currency report.
    base_rate: Decimal | None
    #: Where the rate came from, for the reader who checks it.
    rate_source: str | None

    customer: CustomerBlock
    supplier: SupplierBlock

    quoted_price: Figure
    landed_total: Figure
    gross_margin: Figure
    gross_margin_percent: Decimal | None
    walk_away_margin_percent: Decimal
    comfortable_margin_percent: Decimal
    walk_away_price: Figure

    lines: list[ReportLine]
    total_quantity: Decimal
    total_supplier_amount: Decimal | None
    supplier_currency: str | None

    cost_rows: list[CostRow]

    tax_label: str
    sub_total: Figure
    tax_total: Figure
    total_incl_tax: Figure

    walk_away_ladder: list[WalkAwayRung]
    negotiation: list[NegotiationStep]

    recommendation: str
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ── building it ────────────────────────────────────────────────────────


def build(
    request: QuoteRequest,
    *,
    base_rate: Decimal | None = None,
    rate_source: str | None = None,
    prepared_on: date | None = None,
) -> CostingReport:
    """The report, from the quote as it stands.

    ``base_rate`` is one unit of the quote's currency in :data:`BASE_CURRENCY`.
    Passed in rather than looked up, so this stays a pure function of its
    inputs; the router decides where the rate comes from.
    """
    currency = (request.currency or BASE_CURRENCY).upper()
    if currency == BASE_CURRENCY:
        base_rate, rate_source = None, None
    rate = base_rate if base_rate and base_rate > 0 else None

    def fig(amount: Decimal | None) -> Figure:
        # Converted from the unrounded figure and rounded once on each side, so
        # a price worked out as landed ÷ 0.7 lands on the same cent in AED as
        # it would if the sum had been done in AED.
        amount = amount or _ZERO
        return Figure(_money(amount), _money(amount * rate) if rate else None)

    pack = bidpack.build(request)
    landed_total = pack.landed.total
    sale = _money(request.total_excl_tax)
    total_incl_tax = _money(request.total)
    tax_total = _money(request.tax_total)

    margin = sale - landed_total
    margin_pct = (margin / sale * Decimal(100)) if sale > 0 else None

    walk_away = (
        request.walk_away_margin_percent
        if request.walk_away_margin_percent is not None
        else DEFAULT_WALK_AWAY
    )
    comfortable = (
        request.comfortable_margin_percent
        if request.comfortable_margin_percent is not None
        else DEFAULT_COMFORTABLE
    )
    if comfortable < walk_away:
        comfortable = walk_away

    lines, total_qty, total_supplier, supplier_currency = _lines(
        request, landed_total, fig, rate
    )
    cost_rows = _cost_rows(pack, fig)
    ladder = _walk_away_ladder(landed_total, sale, walk_away, fig)
    steps = _negotiation(sale, total_incl_tax, landed_total, walk_away, comfortable, fig)
    walk_away_price = _price_at_margin(landed_total, walk_away)

    supplier = _supplier(request, supplier_currency)
    recommendation = (request.recommendation or "").strip() or _recommend(
        steps, fig(walk_away_price), currency, comfortable
    )
    notes, warnings = _notes(request, pack, cost_rows, lines, supplier, rate, currency)

    return CostingReport(
        reference=reference_of(request),
        title=request.title,
        prepared_on=prepared_on or date.today(),
        prepared_by=request.created_by.display_name if request.created_by else None,
        reviewed_by=_reviewer(request, ("approve", "reject", "rework")),
        approved_by=_reviewer(request, ("approve",)),
        currency=currency,
        base_currency=BASE_CURRENCY,
        base_rate=rate,
        rate_source=rate_source if rate else None,
        customer=CustomerBlock(
            name=request.customer_name,
            end_user=request.end_user_name or None,
            reference=request.reference_number or request.rfp_number or None,
            portal=request.cf_portal or None,
            place_of_supply=request.place_of_supply or None,
            valid_from=_day(request.quote_date) or _day(request.created_at),
            valid_until=_day(request.expiry_date),
        ),
        supplier=supplier,
        quoted_price=fig(sale),
        landed_total=fig(landed_total),
        gross_margin=fig(margin),
        gross_margin_percent=_pct(margin_pct),
        walk_away_margin_percent=_pct(walk_away),
        comfortable_margin_percent=_pct(comfortable),
        walk_away_price=fig(walk_away_price),
        lines=lines,
        total_quantity=total_qty,
        total_supplier_amount=total_supplier,
        supplier_currency=supplier_currency,
        cost_rows=cost_rows,
        tax_label=_tax_label(request),
        sub_total=fig(sale),
        tax_total=fig(tax_total),
        total_incl_tax=fig(total_incl_tax),
        walk_away_ladder=ladder,
        negotiation=steps,
        recommendation=recommendation,
        notes=notes,
        warnings=warnings,
    )


def _reviewer(request: QuoteRequest, actions: tuple[str, ...]) -> str | None:
    """The name behind the latest review of one of these kinds, or None."""
    for review in reversed(list(request.reviews or [])):
        if str(review.action) in actions:
            return review.reviewer.display_name if review.reviewer else None
    return None


def reference_of(request: QuoteRequest) -> str:
    """What the report is headed with: our reference, or the bid's, or a
    short handle nobody has to invent."""
    return (
        request.reference
        or request.bid_reference
        or request.rfp_number
        or f"QR-{str(request.id)[:8].upper()}"
    )


def _day(value) -> date | None:
    if value is None:
        return None
    return value.date() if isinstance(value, datetime) else value


def _price_at_margin(landed: Decimal, margin_percent: Decimal) -> Decimal:
    """The selling price at which the margin, as a share of that price, is
    ``margin_percent``: landed ÷ (1 − m)."""
    keep = Decimal(1) - margin_percent / Decimal(100)
    if keep <= 0:
        return landed
    # Unrounded: ``fig`` rounds it once on each side.
    return landed / keep


def _lines(request, landed_total, fig, rate):
    """The per-line table, with the landed cost allocated by cost share."""
    sources = supplier_prices(request)
    items = sorted(request.items, key=lambda i: i.position or 0)
    # Keyed by the object rather than the row id: a line that has not been
    # flushed yet has no id, and every such line would share one key.
    costs = {
        id(i): (i.cost_rate or _ZERO) * (i.quantity or _ZERO)
        for i in items
        if i.cost_rate is not None and (i.cost_rate or _ZERO) > 0
    }
    goods = sum(costs.values(), _ZERO)
    landed_base = _money(landed_total * rate) if rate else None

    # Allocated in proportion to cost, in each currency separately, so the base
    # column adds up to the base total rather than to a converted residue.
    allocated: dict[int, tuple[Decimal, Decimal | None]] = {}
    if goods > 0:
        for item_id, cost in costs.items():
            share = cost / goods
            allocated[item_id] = (
                _money(landed_total * share),
                _money(landed_base * share) if landed_base is not None else None,
            )
        # Rounding leaves a cent over or under; the largest line absorbs it so
        # the column adds up to the total printed under it.
        if allocated:
            biggest = max(allocated, key=lambda k: costs[k])
            over = _money(landed_total) - sum((a for a, _ in allocated.values()), _ZERO)
            over_base = (
                landed_base - sum((b or _ZERO for _, b in allocated.values()), _ZERO)
                if landed_base is not None
                else None
            )
            amount, base = allocated[biggest]
            allocated[biggest] = (
                amount + over,
                (base + over_base) if base is not None and over_base is not None else base,
            )

    # The supplier's own figures, in their own currency, when every costed line
    # came off one supplier document. Otherwise our recorded cost, in ours.
    def source_of(item):
        return sources.get(str(item.id)) if item.id is not None else None

    currencies = {source_of(i)[1] for i in items if source_of(i) is not None}
    from_document = len(currencies) == 1 and all(
        source_of(i) is not None for i in items if id(i) in costs
    )
    supplier_currency = (
        next(iter(currencies)) if from_document else (request.currency or BASE_CURRENCY).upper()
    ) if costs else None

    lines: list[ReportLine] = []
    total_supplier = _ZERO if costs else None
    total_qty = _ZERO
    for index, item in enumerate(items, start=1):
        key = id(item)
        qty = item.quantity or _ZERO
        total_qty += qty
        selling = fig(item.line_total)

        supplier_amount = None
        if from_document and (found := source_of(item)) is not None:
            unit, _ = found
            supplier_amount = _money(unit * qty)
        elif not from_document and key in costs:
            supplier_amount = _money(costs[key])
        if supplier_amount is not None and total_supplier is not None:
            total_supplier += supplier_amount

        landed = margin_fig = None
        margin_pct = None
        if key in allocated:
            amount, base = allocated[key]
            landed = Figure(amount, base)
            margin_fig = Figure(
                selling.amount - amount,
                (selling.base - base) if selling.base is not None and base is not None else None,
            )
            if selling.amount > 0:
                margin_pct = _pct(margin_fig.amount / selling.amount * Decimal(100))

        lines.append(
            ReportLine(
                position=index,
                part_number=item.item_code or None,
                description=item.name,
                quantity=qty,
                unit=item.unit or None,
                supplier_amount=supplier_amount,
                supplier_currency=supplier_currency if supplier_amount is not None else None,
                landed=landed,
                selling=selling,
                margin=margin_fig,
                margin_percent=margin_pct,
            )
        )
    return lines, total_qty, total_supplier, supplier_currency


def _cost_rows(pack, fig) -> list[CostRow]:
    rows: list[CostRow] = []
    for element in pack.landed.elements:
        rated = element.percent is not None
        rows.append(
            CostRow(
                label=(
                    f"{element.label} ({_trim(element.percent)}%)" if rated else element.label
                ),
                amount=fig(element.amount_base),
                is_estimate=not element.is_firm and not element.computed and not rated,
                computed=element.computed or rated,
            )
        )
    return rows


def _trim(value: Decimal | None) -> str:
    if value is None:
        return ""
    text = f"{value:f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _walk_away_ladder(landed, sale, walk_away, fig) -> list[WalkAwayRung]:
    """Three rungs around the walk-away: five points either side of it."""
    rungs: list[WalkAwayRung] = []
    for margin in (walk_away + 5, walk_away, walk_away - 5):
        if margin < 0:
            continue
        price = _price_at_margin(landed, margin)
        discount = (
            _pct((Decimal(1) - price / sale) * Decimal(100)) if sale > 0 else None
        )
        rungs.append(WalkAwayRung(_pct(margin), fig(price), discount))
    return rungs


def _status(margin_pct: Decimal | None, walk_away: Decimal, comfortable: Decimal) -> str:
    """What a step on the ladder leaves, in four plain words.

    Above the comfortable margin the discount can be given without asking
    anybody; down to the walk-away it is acceptable; below the walk-away it
    needs management's approval; and below zero it is a loss — the price no
    longer covers the landed cost, whatever anybody approves.
    """
    if margin_pct is None or margin_pct < 0:
        return LOSS
    if margin_pct < walk_away:
        return NEEDS_APPROVAL
    if margin_pct >= comfortable:
        return COMFORTABLE
    return ACCEPTABLE


def _negotiation(
    sale, total_incl_tax, landed, walk_away, comfortable, fig
) -> list[NegotiationStep]:
    """The quoted price, then each discount on the ladder and what it leaves."""
    steps: list[NegotiationStep] = []
    for discount in (_ZERO, *DISCOUNT_LADDER):
        keep = Decimal(1) - discount / Decimal(100)
        sale_d = _money(sale * keep)
        total_d = _money(total_incl_tax * keep)
        margin = sale_d - landed
        pct = (margin / sale_d * Decimal(100)) if sale_d > 0 else None
        steps.append(
            NegotiationStep(
                discount_percent=_pct(discount),
                total_incl_tax=fig(total_d),
                selling=fig(sale_d),
                margin=fig(margin),
                margin_percent=_pct(pct),
                status=_status(pct, walk_away, comfortable),
            )
        )
    return steps


def _recommend(steps, floor: Figure, currency: str, comfortable: Decimal) -> str:
    """What to say if the customer asks for a discount, from the ladder."""
    easy = [s for s in steps if s.discount_percent > 0 and s.status == COMFORTABLE]
    floor_text = f"{currency} {floor.amount:,.2f}"
    if floor.base is not None:
        floor_text += f" / {BASE_CURRENCY} {floor.base:,.2f}"
    if not easy:
        return (
            f"Hold the quoted price — any discount takes the margin below "
            f"{_trim(comfortable)}%. Do not go below {floor_text} ex-VAT."
        )
    offers = [f"{_trim(s.discount_percent)}%" for s in easy]
    if len(offers) == 1:
        opening = f"Counter at {offers[0]} as the final offer"
    else:
        opening = (
            "Counter at " + ", then ".join(offers[:-1]) + f"; {offers[-1]} as the final offer"
        )
    return (
        f"{opening}. Anything past that needs management approval. "
        f"Do not go below {floor_text} ex-VAT."
    )


def _supplier(request, supplier_currency: str | None) -> SupplierBlock:
    """The supplier as the report names them: what was typed, else the offer
    the quote is priced from."""
    chosen = None
    if request.comparison is not None and request.selected_supplier_quote_id is not None:
        chosen = next(
            (q for q in request.comparison.quotes if q.id == request.selected_supplier_quote_id),
            None,
        )
    return SupplierBlock(
        name=(request.supplier_name or "").strip() or (chosen.supplier_name if chosen else None),
        basis=(request.supplier_basis or "").strip() or None,
        route=(request.supplier_route or "").strip()
        or (
            f"{request.mode_of_shipment} to {request.incoterm_place}"
            if request.mode_of_shipment and request.incoterm_place
            else None
        ),
        currency=supplier_currency or (chosen.currency if chosen else None),
        quote_number=chosen.quote_number if chosen else None,
        creator=request.created_by.display_name if request.created_by else None,
    )


def _tax_label(request) -> str:
    name = (request.tax_name or "").strip() or "VAT"
    if request.tax_percentage is not None and request.tax_percentage > 0:
        return f"{name} {_trim(request.tax_percentage)}%"
    return name


def _notes(request, pack, cost_rows, lines, supplier, rate, currency):
    """The footnotes: what is an estimate, what is not in the cost, and what
    the person raising the quote wanted the approver told."""
    notes: list[str] = [
        "Selling price = cost ÷ (1 − margin). Every margin here is a share of the "
        "selling price, not a markup on cost."
    ]
    warnings: list[str] = []

    estimates = [r.label for r in cost_rows if r.is_estimate]
    if estimates:
        who = supplier.name or "the supplier"
        notes.append(
            f"* {_join(estimates)} are estimates – confirm against {who}'s checkout "
            f"and the courier's invoice before the PO."
        )
    if request.tax_total > 0:
        notes.append("Import VAT is recoverable, so it is not included in the cost.")
    if rate is None and currency != BASE_CURRENCY:
        warnings.append(
            f"Figures are in {currency} only — no {BASE_CURRENCY} rate was available "
            f"when this was prepared."
        )
    uncosted = [line for line in lines if line.landed is None]
    if uncosted and lines:
        warnings.append(
            f"{len(uncosted)} of {len(lines)} lines carry no supplier cost, so their "
            f"landed cost and margin are not shown and the totals understate the cost."
        )
    if pack.landed.total <= 0:
        warnings.append(
            "No cost is recorded behind this quote; the margin shown is the whole price."
        )
    for raw in (request.report_notes or "").splitlines():
        line = raw.strip()
        if line:
            notes.append(line)
    return notes, warnings


def _join(labels: list[str]) -> str:
    short = [label.split(" (")[0] for label in labels]
    if len(short) == 1:
        return short[0]
    return ", ".join(short[:-1]) + " and " + short[-1]
