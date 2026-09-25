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

from app.core.config import Settings
from app.models.comparison import SupplierQuote
from app.models.quoting import CostStage, QuoteCostLine, QuoteRequest

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


def _mentions(text: str | None, hints: tuple[str, ...]) -> bool:
    lowered = (text or "").strip().lower()
    return bool(lowered) and any(hint in lowered for hint in hints)


def is_import(request: QuoteRequest, quote: SupplierQuote | None) -> bool:
    """Whether the goods have to be brought in.

    Decided by what the documents say — an Incoterm on the offer that hands
    the goods over abroad (EXW, FOB, CIF…), or a route or a basis on the
    quote that names a courier, a freight mode or an overseas purchase — and
    never by the currency alone. It used to be: a Redington in Dubai quoting
    in dollars was treated as an import, and 1% insurance and 5% duty were
    put on a price that carries neither.
    """
    if quote is not None:
        term = (quote.incoterms or "").strip().split()[:1]
        if term and term[0].upper() in _ABROAD_TERMS:
            return True
    return _mentions(request.supplier_route, _FROM_ABROAD) or _mentions(
        request.supplier_basis, _FROM_ABROAD
    )


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

    # What the supplier quoted for shipping, exactly as they quoted it.
    if quote.freight is not None and quote.freight > 0:
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

    importing = is_import(request, quote)
    if importing and settings.costing_default_insurance_percent > 0:
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
    if (
        importing
        and settings.costing_default_duty_percent > 0
        and not (request.customs_duty_percent or _ZERO) > 0
    ):
        request.customs_duty_percent = settings.costing_default_duty_percent
        added.append("duty")

    if pays_up_front(request, quote) and settings.costing_default_bank_charge_percent > 0:
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
