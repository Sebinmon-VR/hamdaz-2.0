"""What choosing a supplier fills in: the freight they quoted, VAT on the
total, duty and insurance on an import, the bank's cut on a card purchase.

No database. The seeding is a function of the request, the chosen supplier
quote and the house defaults, so it is attacked directly.
"""

from __future__ import annotations

from decimal import Decimal

from app.core.config import get_settings
from app.models.comparison import SupplierQuote
from app.models.quoting import (
    CostStage,
    DocumentKind,
    QuoteCostLine,
    QuoteDocument,
    QuoteRequest,
    QuoteRequestItem,
)
from app.quoting import costing, reading


def house(**overrides):
    settings = get_settings().model_copy()
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


def request_with(**overrides) -> QuoteRequest:
    fields = {
        "title": "Drives", "customer_name": "ADNOC", "currency": "USD",
        "customs_duty_percent": Decimal(0), "financing_rate_percent": Decimal(0),
        "cash_exposure_days": 0, "discloses_principal_price": False,
        "multiple_supplier_quotes": False, "discount": Decimal(0),
        "shipping_charge": Decimal(0), "adjustment": Decimal(0), **overrides,
    }
    request = QuoteRequest(**fields)
    request.items = [
        QuoteRequestItem(position=0, name="HPE 2.4TB", quantity=Decimal(1), rate=Decimal("2125.70"),
                         cost_rate=Decimal(637), discount=Decimal(0)),
    ]
    request.cost_lines, request.compliance = [], []
    request.submission_fields, request.documents = [], []
    return request


def supplier(**overrides) -> SupplierQuote:
    fields = {"supplier_name": "router-switch.com", "currency": "USD", **overrides}
    quote = SupplierQuote(**fields)
    quote.items = []
    return quote


# ── seeding the build-up ───────────────────────────────────────────────


def test_an_overseas_card_purchase_gets_freight_insurance_duty_and_bank_charges() -> None:
    """The reference quote: bought online in dollars, shipped by courier."""
    request = request_with(supplier_basis="online purchase")
    quote = supplier(freight=Decimal("60"), payment_terms="100% in advance by card",
                     incoterms="EXW Shenzhen")

    added = costing.seed_costing(request, quote, house())

    assert added == ["freight", "insurance", "duty", "bank charges"]
    rows = {row.label: row for row in request.cost_lines}
    freight = rows["Freight – router-switch.com"]
    assert (freight.stage, freight.is_firm, freight.amount_base) == (
        CostStage.ORIGIN, True, Decimal("60.00"),
    )
    assert freight.basis == "Supplier quotation"
    assert (rows["Insurance"].percent, rows["Insurance"].percent_of) == (Decimal(1), "goods")
    assert rows["Insurance"].basis == costing.HOUSE_DEFAULT and rows["Insurance"].is_firm is False
    bank = rows["Payment / bank charges"]
    assert (bank.stage, bank.percent, bank.percent_of) == (
        CostStage.DESTINATION, Decimal(3), "goods",
    )
    assert request.customs_duty_percent == Decimal(5)


def test_the_seeded_costing_reproduces_the_reference_landed_cost() -> None:
    """Goods 1,280 + freight 60 + insurance 1% + duty 5% of CIF + bank 3%:
    the same 1,503.84 the hand-prepared report reached, before clearance and
    local delivery, which nobody's document states."""
    from app.quoting import bidpack

    request = request_with(supplier_basis="online purchase")
    request.items = [
        QuoteRequestItem(position=i, name=n, quantity=Decimal(1), rate=Decimal(0),
                         cost_rate=Decimal(c), discount=Decimal(0))
        for i, (n, c) in enumerate((("a", "637"), ("b", "181"), ("c", "462")))
    ]
    costing.seed_costing(request, supplier(freight=Decimal("60"), incoterms="EXW"), house())

    landed = bidpack.landed_cost(request)
    assert landed.cif_subtotal == Decimal("1352.80")
    assert landed.customs_duty == Decimal("67.64")
    assert landed.total == Decimal("1503.84") - Decimal("30") - Decimal("15")


def test_a_foreign_freight_figure_is_converted_at_the_bids_rate() -> None:
    request = request_with(currency="USD", supplier_currency="AED", fx_rate=Decimal("3.6725"))
    quote = supplier(currency="AED", freight=Decimal("220.35"))

    costing.seed_costing(request, quote, house())

    freight = request.cost_lines[0]
    assert (freight.amount_source, freight.source_currency) == (Decimal("220.35"), "AED")
    assert freight.amount_base == Decimal("60.00")


def test_a_local_supplier_with_no_freight_seeds_nothing() -> None:
    request = request_with(currency="AED")
    quote = supplier(currency="AED", payment_terms="30 days net")

    assert costing.seed_costing(request, quote, house()) == []
    assert request.cost_lines == []
    assert request.customs_duty_percent == Decimal(0)


def test_nothing_is_seeded_over_a_build_up_somebody_already_made() -> None:
    request = request_with()
    request.cost_lines = [
        QuoteCostLine(
            position=1, stage=CostStage.ORIGIN, label="Air freight", amount_base=Decimal(850)
        )
    ]
    quote = supplier(freight=Decimal("60"), incoterms="EXW")

    assert costing.seed_costing(request, quote, house()) == []
    assert [r.label for r in request.cost_lines] == ["Air freight"]


def test_a_typed_duty_rate_is_left_alone() -> None:
    request = request_with(customs_duty_percent=Decimal("0"))
    request.customs_duty_percent = Decimal("12")
    costing.seed_costing(request, supplier(incoterms="FOB"), house())
    assert request.customs_duty_percent == Decimal("12")


def test_a_zero_default_switches_that_default_off() -> None:
    request = request_with(supplier_basis="online")
    quote = supplier(freight=Decimal("60"), incoterms="EXW")
    added = costing.seed_costing(
        request, quote, house(costing_default_insurance_percent=Decimal(0),
                              costing_default_bank_charge_percent=Decimal(0)),
    )
    assert added == ["freight", "duty"]


# ── tax on the total ───────────────────────────────────────────────────


def test_vat_goes_on_the_quote_that_has_none() -> None:
    request = request_with()
    assert costing.seed_tax(request, house()) == 1
    assert (request.tax_percentage, request.tax_name) == (Decimal(5), "VAT")
    # Once on, it is somebody's decision; seeding again changes nothing.
    request.tax_percentage = Decimal("7.5")
    assert costing.seed_tax(request, house()) == 0
    assert request.tax_percentage == Decimal("7.5")


def test_a_zero_rated_quote_is_left_zero_rated() -> None:
    request = request_with(tax_percentage=Decimal(0), tax_name="Zero")
    assert costing.seed_tax(request, house()) == 0
    assert (request.tax_percentage, request.tax_name) == (Decimal(0), "Zero")


def test_a_zero_house_rate_puts_no_tax_on() -> None:
    request = request_with()
    assert costing.seed_tax(request, house(costing_default_tax_percent=Decimal(0))) == 0
    assert request.tax_percentage is None


# ── an RFQ that states the tax, the duty and the terms ─────────────────

RFQ = b"""ADNOC Onshore
RFQ No: 6000150626
Prices to be quoted exclusive of VAT 5%.
Customs duty at 5% to be included in the unit rates.
Payment terms: 60 days from invoice
Delivery period: 6 weeks from PO
Item,Description,Qty
1,Drive,4
"""


async def test_an_rfq_offers_its_tax_duty_and_terms() -> None:
    request = request_with()
    document = QuoteDocument(kind=DocumentKind.CUSTOMER_RFQ, file_name="rfq.csv")
    await reading.read_into(document, request, "rfq.csv", RFQ, "text/csv")

    s = document.suggestions
    assert s["tax_percentage"]["value"] == "5"
    assert s["customs_duty_percent"]["value"] == "5"
    assert s["payment_terms"]["value"] == "60 days from invoice"
    assert s["delivery_terms"]["value"] == "6 weeks from PO"


async def test_applying_the_tax_puts_it_on_the_total() -> None:
    request = request_with()
    document = QuoteDocument(kind=DocumentKind.CUSTOMER_RFQ, file_name="rfq.csv")
    await reading.read_into(document, request, "rfq.csv", RFQ, "text/csv")

    reading.apply(request, document, ["tax_percentage", "customs_duty_percent", "payment_terms"])

    assert (request.tax_percentage, request.tax_name) == (Decimal(5), "VAT")
    assert request.customs_duty_percent == Decimal(5)
    assert request.payment_terms == "60 days from invoice"
    # The quote's own total now carries the tax the customer expects:
    # 5% of 2,125.70 = 106.285, rounded half up, once.
    assert request.tax_total == Decimal("106.29")

def test_a_dollar_offer_from_a_local_distributor_is_not_an_import() -> None:
    """Redington in Dubai quotes in dollars. No Incoterm handing the goods
    over abroad, no courier on the route: no duty, no insurance."""
    request = request_with(currency="AED")
    quote = supplier(currency="USD", payment_terms="30 days net")

    assert costing.is_import(request, quote) is False
    assert costing.seed_costing(request, quote, house()) == []
    assert request.customs_duty_percent == Decimal(0)


def test_a_courier_on_the_route_makes_it_an_import() -> None:
    request = request_with(supplier_route="Express courier, Shenzhen to Abu Dhabi")
    quote = supplier(currency="USD")

    assert costing.is_import(request, quote) is True
    assert costing.seed_costing(request, quote, house()) == ["insurance", "duty"]
