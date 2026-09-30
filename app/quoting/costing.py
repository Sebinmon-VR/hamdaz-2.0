"""Filling the costing in from what the documents and the house already know.

A quote priced from a supplier used to arrive with its goods costed and
nothing else: no freight, no tax on the total, no duty, an empty landed-cost
sheet for somebody to type into from memory. The information was mostly
already there — the supplier's quotation states a freight charge, an overseas
supplier means duty, a card purchase means the bank's cut — and a person was
retyping it.

So the moment a supplier is chosen, this puts in what follows from the
documents and from the house's own rules, plainly labelled as which:

* **Freight the supplier quoted** becomes a firm cost row, in their currency,
  converted at the bid's rate.
* **An import** — the supplier hands the goods over abroad (EXW, FOB, CIF…),
  or the route or basis names a courier, a freight mode or an overseas
  purchase — gets the house insurance rate as a rated row and the house duty
  rate on the bid, both marked as defaults to edit. Never on the currency
  alone: a Dubai distributor quotes in dollars too.
* **An online or advance-payment purchase** gets the house bank-charge rate.
* **VAT** at the house rate goes on the quote's total, where it has none.

Nothing here overwrites a figure somebody typed. Rows are only seeded into an
empty build-up, duty only onto a bid with none, and tax only onto a quote
with none. Every seeded row says where it came from, and every one can be edited
or removed on the landed cost sheet like any other.
"""

from __future__ import annotations

import logging
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from app.comparison.charges import LABELS
from app.comparison.schemas import ChargeIn
from app.core.config import Settings
from app.models.comparison import SupplierQuote
from app.models.quoting import CostStage, QuoteCostLine, QuoteRequest, TradeDirection

logger = logging.getLogger("hamdaz.quoting")

_PRICE: Final = Decimal("0.01")
_ZERO: Final = Decimal(0)

#: Incoterms under which the goods are still abroad when we take them on, so
#: the shipping, insurance and duty are ours.
_ABROAD_TERMS: Final = frozenset({"EXW", "FCA", "FAS", "FOB", "CFR", "CIF", "CPT", "CIP"})

#: Wording on the route or the basis that means the goods come in from
#: abroad, so duty and insurance are real costs rather than an invention.
_FROM_ABROAD: Final = (
    "import", "overseas", "abroad", "courier", "express", "air freight",
    "sea freight", "dhl", "fedex", "aramex", "ex works", "ex-works",
)

#: Wording that means we pay by card or before dispatch — where a bank charge
#: or a card fee comes off the top.
_PAY_UP_FRONT: Final = (
    "advance", "pre-payment", "prepayment", "proforma", "pro forma", "card",
    "100% with order", "cash with order", "cwo", "before dispatch", "online",
)

HOUSE_DEFAULT: Final = "House default — edit or remove"

#: Wording that says the goods are going *out* — to a customer abroad. Read
#: from where the goods are going and how the deal is described, never from
#: the customer's name: a Saudi company buying for its Dubai office is a local
#: delivery, and only the delivery address knows that.
_GOING_ABROAD: Final = ("export", "re-export", "re export", "for export")


def _mentions(text: str | None, hints: tuple[str, ...]) -> bool:
    lowered = (text or "").strip().lower()
    return bool(lowered) and any(hint in lowered for hint in hints)


def chosen_supplier_quote(request: QuoteRequest) -> SupplierQuote | None:
    """The offer the quote is priced from, out of the comparison on it."""
    chosen = request.selected_supplier_quote_id
    if chosen is None or request.comparison is None:
        return None
    return next((q for q in request.comparison.quotes if q.id == chosen), None)


def detect_direction(
    request: QuoteRequest, quote: SupplierQuote | None
) -> tuple[str | None, str]:
    """What the documents say about the border, and why — the automatic half.

    An Incoterm on the offer that hands the goods over abroad (EXW, FOB, CIF…),
    or a route or a basis on the quote that names a courier, a freight mode or
    an overseas purchase, reads as an import. Wording that says the goods are
    going out for export reads as an export. Never the currency alone: a
    Redington in Dubai quotes in dollars too, and treating that as an import
    put 1% insurance and 5% duty on a price that carries neither.

    Returns ``(None, reason)`` when nothing on the documents says either way.
    That is an honest answer, not a default — the costing then seeds no duty,
    and the screen says the direction has not been read rather than guessing
    "local" and printing it on the approver's report as a fact.
    """
    if quote is not None:
        term = (quote.incoterms or "").strip().split()[:1]
        if term and term[0].upper() in _ABROAD_TERMS:
            return (
                TradeDirection.IMPORT,
                f"The supplier's offer is {term[0].upper()}: the goods are handed "
                "over abroad, so bringing them in is ours to pay for.",
            )
    for label, text in (("route", request.supplier_route), ("basis", request.supplier_basis)):
        if _mentions(text, _FROM_ABROAD):
            return (
                TradeDirection.IMPORT,
                f"The supplier {label} reads as an overseas purchase: “{text.strip()}”.",
            )
    for label, text in (
        ("delivery terms", request.delivery_terms),
        ("ship-to", request.ship_to),
        ("route", request.supplier_route),
        ("basis", request.supplier_basis),
    ):
        if _mentions(text, _GOING_ABROAD):
            return (
                TradeDirection.EXPORT,
                f"The {label} says the goods are going out: “{text.strip()}”.",
            )
    return None, "Nothing on the documents says the goods cross a border."


def effective_direction(request: QuoteRequest, quote: SupplierQuote | None) -> str | None:
    """The direction the costing uses: a person's word first, then the reading.

    The stored column is what somebody chose on the form. It wins outright,
    because the automatic reading is a guess from wording and the person
    typing the quote has the documents in front of them. Null there means
    nobody has said, and the documents decide.
    """
    stated = (request.trade_direction or "").strip().lower()
    if stated in {d.value for d in TradeDirection}:
        return stated
    detected, _ = detect_direction(request, quote)
    return detected


def is_import(request: QuoteRequest, quote: SupplierQuote | None) -> bool:
    """Whether the goods have to be brought in — what duty and insurance hang on.

    A person's own answer on the quote settles it; otherwise the documents
    do, by the reading in :func:`detect_direction`. A quote somebody marked
    "local" seeds no duty however the supplier's offer is worded, and one
    marked "import" gets it even when the offer says nothing.
    """
    return effective_direction(request, quote) == TradeDirection.IMPORT


def pays_up_front(request: QuoteRequest, quote: SupplierQuote | None) -> bool:
    return _mentions(request.supplier_basis, _PAY_UP_FRONT) or (
        quote is not None and _mentions(quote.payment_terms, _PAY_UP_FRONT)
    )


def seed_costing(
    request: QuoteRequest, quote: SupplierQuote, settings: Settings
) -> list[str]:
    """Put the build-up in, when there is none. Returns what was added."""
    if request.cost_lines:
        return []
    added: list[str] = []
    rows: list[QuoteCostLine] = []
    ours = (request.currency or "").upper()
    theirs = (quote.currency or "").upper() or ours
    fx = request.fx_rate if request.fx_rate and request.fx_rate > 0 else None
    converting = bool(theirs and ours and theirs != ours)

    def to_base(amount: Decimal) -> Decimal:
        if converting and fx:
            return (amount / fx).quantize(_PRICE, rounding=ROUND_HALF_UP)
        if converting:
            return _ZERO
        return amount.quantize(_PRICE, rounding=ROUND_HALF_UP)

    # What the supplier quoted for shipping, exactly as they quoted it —
    # unless somebody has already typed the freight on the freight form, in
    # which case that is the answer and a seeded row would count it twice.
    if request.freight_charges is None and quote.freight is not None and quote.freight > 0:
        if converting and fx:
            base = (quote.freight / fx).quantize(_PRICE, rounding=ROUND_HALF_UP)
        elif converting:
            base = _ZERO
        else:
            base = quote.freight.quantize(_PRICE, rounding=ROUND_HALF_UP)
        rows.append(
            QuoteCostLine(
                position=len(rows) + 1,
                stage=CostStage.ORIGIN,
                label=f"Freight – {quote.supplier_name}",
                basis="Supplier quotation",
                amount_source=quote.freight if converting else None,
                source_currency=theirs if converting else None,
                amount_base=base,
                is_principal=False,
                is_firm=True,
                notes=(
                    "As stated on the supplier's quotation."
                    + ("" if base or not converting else " No rate on the bid yet to convert it.")
                ),
            )
        )
        added.append("freight")

    # Everything else the supplier said is on top — duty, handling, insurance,
    # clearance — often only in the notes. What they stated beats the house
    # default for the same thing, so those are settled first.
    stated = {c.kind: c for c in charges_of(quote)}
    duty_settled = False
    for charge in stated.values():
        if charge.kind == "duty":
            duty_settled = _seed_duty(request, charge, quote, rows, to_base, theirs, converting)
            continue
        rows.append(_charge_row(charge, quote, len(rows) + 1, to_base, theirs, converting))
        added.append(charge.kind)
    if duty_settled:
        added.append("duty")

    importing = is_import(request, quote)
    if (
        importing
        and settings.costing_default_insurance_percent > 0
        and "insurance" not in stated
    ):
        rows.append(
            QuoteCostLine(
                position=len(rows) + 1,
                stage=CostStage.ORIGIN,
                label="Insurance",
                basis=HOUSE_DEFAULT,
                amount_base=_ZERO,
                is_principal=False,
                is_firm=False,
                percent=settings.costing_default_insurance_percent,
                percent_of="goods",
            )
        )
        added.append("insurance")
    # A supplier who says duty is extra but gives no rate is saying this is an
    # import, whatever the Incoterm says: the house rate is the allowance.
    duty_extra = "duty" in stated and not stated["duty"].included
    if (
        (importing or duty_extra)
        and not duty_settled
        and settings.costing_default_duty_percent > 0
        and not (request.customs_duty_percent or _ZERO) > 0
        # A duty figure on the freight form is the agent's own number; a
        # house rate beside it would be ignored by the build-up anyway, and
        # would read on the sheet as a second answer.
        and request.duty_charges is None
    ):
        request.customs_duty_percent = settings.costing_default_duty_percent
        added.append("duty")

    if (
        pays_up_front(request, quote)
        and settings.costing_default_bank_charge_percent > 0
        and "bank" not in stated
    ):
        rows.append(
            QuoteCostLine(
                position=len(rows) + 1,
                stage=CostStage.DESTINATION,
                label="Payment / bank charges",
                basis=HOUSE_DEFAULT,
                amount_base=_ZERO,
                is_principal=False,
                is_firm=False,
                percent=settings.costing_default_bank_charge_percent,
                percent_of="goods",
            )
        )
        added.append("bank charges")

    if rows:
        request.cost_lines = rows
    if added:
        logger.info("quote %s: costing seeded with %s", request.id, ", ".join(added))
    return added


#: Paid to the supplier with the goods, so before the duty base; the rest is
#: paid here, after arrival.
_ORIGIN_CHARGES: Final = frozenset({"handling", "packing", "insurance", "documentation"})


def charges_of(quote: SupplierQuote) -> list[ChargeIn]:
    """The charges stored on a supplier quote, read back. Bad rows are skipped."""
    out: list[ChargeIn] = []
    for raw in getattr(quote, "charges", None) or []:
        try:
            out.append(ChargeIn.model_validate(raw))
        except ValueError:
            continue
    return out


def _stated(charge: ChargeIn, supplier: str) -> str:
    return f"{supplier} wrote: “{charge.label}”" if charge.label else f"Stated by {supplier}."


def _charge_row(
    charge: ChargeIn,
    quote: SupplierQuote,
    position: int,
    to_base,
    theirs: str,
    converting: bool,
) -> QuoteCostLine:
    """One charge as a row of the build-up, exactly as the supplier stated it."""
    name = LABELS.get(charge.kind, charge.kind.replace("_", " ").capitalize())
    stage = CostStage.ORIGIN if charge.kind in _ORIGIN_CHARGES else CostStage.DESTINATION
    row = QuoteCostLine(
        position=position,
        stage=stage,
        label=f"{name} – {quote.supplier_name}",
        basis="Supplier quotation",
        amount_base=_ZERO,
        is_principal=False,
        is_firm=False,
        notes=_stated(charge, quote.supplier_name),
    )
    if charge.included:
        row.label = f"{name} – included in {quote.supplier_name}'s price"
        row.is_firm = True
    elif charge.amount:
        amount = Decimal(charge.amount)
        row.amount_base = to_base(amount)
        row.amount_source = amount if converting else None
        row.source_currency = theirs if converting else None
        row.is_firm = True
    elif charge.percent:
        row.percent = Decimal(charge.percent)
        # A rate on the CIF value can only be taken after arrival; before it,
        # the goods are the base (``bidpack.landed_cost``).
        row.percent_of = charge.percent_of if stage is CostStage.DESTINATION else "goods"
    else:
        row.label = f"{name} – {quote.supplier_name} says extra, amount not stated"
        row.notes = (row.notes or "") + " Enter the figure when it is known."
    return row


def _seed_duty(
    request: QuoteRequest,
    charge: ChargeIn,
    quote: SupplierQuote,
    rows: list[QuoteCostLine],
    to_base,
    theirs: str,
    converting: bool,
) -> bool:
    """Duty the supplier stated. True when that settles it and no house rate applies.

    A rate replaces the house rate. A figure is its own firm row, with the rate
    at 0 so the duty is not counted twice. Included (DDP, "inclusive of duty")
    is a 0 row saying so. "Extra" with nothing more is left to the house rate.
    A duty typed on the freight form is the agent's number and wins over all of it.
    """
    if request.duty_charges is not None:
        return True
    if charge.percent:
        request.customs_duty_percent = Decimal(charge.percent)
        return True
    if charge.amount or charge.included:
        request.customs_duty_percent = _ZERO
        rows.append(_charge_row(charge, quote, len(rows) + 1, to_base, theirs, converting))
        return True
    return False


def seed_tax(request: QuoteRequest, settings: Settings) -> int:
    """VAT at the house rate on the quote's total, where it has no rate yet.

    Returns 1 when it was put on and 0 when the quote already had one — a
    zero-rated quote has a rate of 0, which is an answer, and is left alone.
    """
    rate = settings.costing_default_tax_percent
    if rate is None or rate <= 0 or request.tax_percentage is not None:
        return 0
    request.tax_percentage = rate
    request.tax_name = request.tax_name or settings.costing_default_tax_name
    return 1


def set_tax(request: QuoteRequest, percent: Decimal, name: str | None) -> int:
    """The tax on the quote's total, as a person or a document decided."""
    request.tax_percentage = percent
    if name:
        request.tax_name = name
    return 1
