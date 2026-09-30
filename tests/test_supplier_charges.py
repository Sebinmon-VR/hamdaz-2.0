"""Duty and the other charges a supplier states on top, wherever they state them.

The case that was failing: the price table is read, and the notes underneath
say "Customs duty 5% extra", "Handling charges USD 150", "Delivered duty paid",
and none of it reached the costing. No database here: the reader is a function
of text, and the seeding a function of the request and the supplier quote.
"""

from __future__ import annotations

from decimal import Decimal

from app.comparison.charges import find_charges
from app.comparison.documents import Readable
from app.comparison.extraction import with_charges
from app.comparison.parsing import _TOTALS, _amount_on_label_line, parse
from app.models.quoting import CostStage
from app.quoting import costing
from tests.test_quoting_costing import house, request_with, supplier

NOTES = """Item | Description | Qty | Unit price | Total
1 | HPE 2.4TB SAS drive | 4 | 637.00 | 2,548.00
2 | Installation and commissioning of drives | 1 | 500.00 | 500.00
Grand Total USD 3,048.00
Terms and conditions:
1. Prices are ex-works Germany. Customs duty 5% extra.
2. Handling charges: USD 150 per shipment.
3. Insurance 1% of CIF value.
4. Customs clearance at actuals, to be borne by the buyer.
5. Duty as per HS code 8471.30
Delivery by courier within 3-5 days.
Bank charges excluded. Packing included.
"""


def charges(text: str, items: list[str] | None = None) -> dict:
    return {c.kind: c for c in find_charges(text, items)}


# ── reading them ───────────────────────────────────────────────────────


def test_the_notes_under_the_table_are_read_for_charges() -> None:
    found = charges(NOTES, ["HPE 2.4TB SAS drive", "Installation and commissioning of drives"])
    assert found["duty"].percent == 5 and found["duty"].amount is None
    assert found["handling"].amount == 150
    assert (found["insurance"].percent, found["insurance"].percent_of) == (1, "cif")
    # Extra, with no figure: still a cost to allow for.
    clearance = found["clearance"]
    assert clearance.amount is None and clearance.percent is None and not clearance.included
    assert not found["bank"].included
    assert found["packing"].included
    # A priced line is the supplier selling a service, not a charge on top.
    assert "installation" not in found


def test_included_duty_is_read_as_included() -> None:
    for text in (
        "Prices are DDP Dubai, delivered duty paid.",
        "Prices inclusive of customs duty.",
    ):
        assert charges(text)["duty"].included, text
    assert not charges("Duty: not included")["duty"].included


def test_a_code_or_a_duration_is_not_money() -> None:
    assert "duty" not in charges("Duty as per HS code 8471.30")
    assert charges("Customs Duty | | | 1,250.00")["duty"].amount == 1250


def test_a_figure_shared_with_the_freight_is_the_freight() -> None:
    found = charges("Freight & handling USD 300")
    assert "handling" not in found


def test_freight_is_not_a_delivery_time_or_a_rate() -> None:
    freight = _TOTALS["freight"]
    assert _amount_on_label_line("Delivery by courier within 3-5 days", freight) is None
    assert _amount_on_label_line("Freight 10% extra", freight) is None
    assert _amount_on_label_line("Freight charges: USD 1,250.00", freight) == Decimal("1250.00")


def test_a_duty_row_in_the_price_table_is_not_an_item() -> None:
    text = (
        "Description | Qty | Unit price | Total\n"
        "HPE 2.4TB SAS drive | 4 | 637.00 | 2,548.00\n"
        "Customs duty | 1 | 127.40 | 127.40\n"
        "Total USD 2,675.40\n"
    )
    readable = Readable(
        file_name="offer.csv",
        kind="text",
        media_type="text/csv",
        text=text,
        tables=[[row.split(" | ") for row in text.splitlines()[:3]]],
    )
    quote = with_charges(parse(readable), readable.text)
    assert [i.description for i in quote.items] == ["HPE 2.4TB SAS drive"]
    assert [(c.kind, c.amount) for c in quote.charges] == [("duty", 127.4)]
    assert "Customs duty" in quote.note


# ── costing them ───────────────────────────────────────────────────────


def stated(*rows: dict) -> list[dict]:
    base = {"label": "", "amount": None, "percent": None, "percent_of": "goods", "included": False}
    return [{**base, **row} for row in rows]


def test_a_stated_duty_rate_replaces_the_house_rate() -> None:
    request = request_with()
    quote = supplier(incoterms="EXW", charges=stated({"kind": "duty", "percent": "12.5"}))
    costing.seed_costing(request, quote, house())
    assert request.customs_duty_percent == Decimal("12.5")


def test_a_duty_figure_is_its_own_row_and_not_counted_twice() -> None:
    request = request_with()
    quote = supplier(incoterms="EXW", charges=stated({"kind": "duty", "amount": "320"}))
    costing.seed_costing(request, quote, house())
    assert request.customs_duty_percent == 0
    row = next(r for r in request.cost_lines if "duty" in r.label.lower())
    assert (row.stage, row.amount_base, row.is_firm) == (CostStage.DESTINATION, Decimal("320.00"), True)


def test_duty_included_means_no_house_duty() -> None:
    request = request_with()
    quote = supplier(incoterms="EXW", charges=stated({"kind": "duty", "included": True}))
    costing.seed_costing(request, quote, house())
    assert request.customs_duty_percent == 0
    assert any("included" in r.label for r in request.cost_lines)


def test_duty_extra_with_no_rate_gets_the_house_rate_even_locally() -> None:
    # Nothing says import (no Incoterm, no route), but the supplier says duty
    # is extra: that is an allowance to make.
    request = request_with()
    quote = supplier(charges=stated({"kind": "duty"}))
    costing.seed_costing(request, quote, house(costing_default_duty_percent=Decimal(5)))
    assert request.customs_duty_percent == Decimal(5)


def test_other_charges_become_rows_of_the_build_up() -> None:
    request = request_with()
    quote = supplier(
        incoterms="EXW",
        charges=stated(
            {"kind": "handling", "amount": "150", "label": "Handling charges: USD 150"},
            {"kind": "insurance", "percent": "0.5"},
            {"kind": "clearance"},
        ),
    )
    costing.seed_costing(request, quote, house())
    by_label = {r.label: r for r in request.cost_lines}
    handling = by_label["Handling – router-switch.com"]
    assert (handling.stage, handling.amount_base, handling.is_firm) == (
        CostStage.ORIGIN, Decimal("150.00"), True,
    )
    assert "Handling charges: USD 150" in handling.notes
    insurance = by_label["Insurance – router-switch.com"]
    assert insurance.percent == Decimal("0.5")
    # The supplier's insurance, not the house default beside it.
    assert "Insurance" not in by_label
    clearance = by_label["Customs clearance – router-switch.com says extra, amount not stated"]
    assert (clearance.stage, clearance.amount_base, clearance.is_firm) == (
        CostStage.DESTINATION, Decimal(0), False,
    )


def test_a_charge_in_another_currency_is_converted_at_the_bid_rate() -> None:
    request = request_with(currency="AED", fx_rate=Decimal("0.2723"))
    quote = supplier(currency="USD", charges=stated({"kind": "handling", "amount": "100"}))
    costing.seed_costing(request, quote, house())
    row = request.cost_lines[0]
    assert (row.amount_source, row.source_currency) == (Decimal(100), "USD")
    assert row.amount_base == Decimal("367.24")
