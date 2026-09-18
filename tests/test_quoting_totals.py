"""The three totals a customer reads, and how tax gets into the last one.

Pure arithmetic on the model — no database, no routes — because that is all it
is, and a four-minute route test to check an addition is the wrong tool.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from app.models.quoting import QuoteRequest, QuoteRequestItem


def _quote(**money) -> QuoteRequest:
    return QuoteRequest(
        title="t",
        customer_name="c",
        discount=Decimal(money.get("discount", 0)),
        shipping_charge=Decimal(money.get("shipping", 0)),
        adjustment=Decimal(money.get("adjustment", 0)),
        items=[],
    )


def _line(qty: str, rate: str, tax: str | None, discount: str = "0") -> QuoteRequestItem:
    return QuoteRequestItem(
        name="x",
        quantity=Decimal(qty),
        rate=Decimal(rate),
        discount=Decimal(discount),
        tax_percentage=None if tax is None else Decimal(tax),
    )


def test_tax_is_added_on_top_of_the_total_and_shown_on_its_own() -> None:
    quote = _quote(discount=100, shipping=50)
    quote.items.append(_line("20", "4553", "5"))   # 91,060 → 4,553 tax
    quote.items.append(_line("1", "100", None))    # untaxed line

    assert quote.sub_total == Decimal("91160")
    assert quote.total_excl_tax == Decimal("91110")     # less 100, plus 50
    assert quote.tax_total == Decimal("4553.00")        # only the taxed line
    assert quote.total == Decimal("95663.00")           # excl. tax + tax


def test_a_quote_with_no_tax_totals_exactly_as_before() -> None:
    quote = _quote(discount=1000, shipping=250)
    quote.items.append(_line("2", "12000", None))
    quote.items.append(_line("2", "4000", None))

    assert quote.tax_total == Decimal("0.00")
    assert quote.total_excl_tax == quote.total == Decimal(32000 - 1000 + 250)


def test_tax_rounds_on_each_line_as_zoho_does() -> None:
    quote = _quote()
    # 3 × 0.333... each round to 0.33 on the line, so the total is 0.99 —
    # which is what Zoho's estimate would show, and matching it is the point.
    for _ in range(3):
        quote.items.append(_line("1", "6.666666", "5"))
    assert [i.tax_amount for i in quote.items] == [Decimal("0.33")] * 3
    assert quote.tax_total == Decimal("0.99")


def test_a_line_carries_zohos_three_columns() -> None:
    item = _line("300", "16.008", "5")
    assert item.line_total == Decimal("4802.4")        # taxable amount
    assert item.tax_amount == Decimal("240.12")        # tax, to the cent
    assert item.total_incl_tax == Decimal("5042.52")   # amount


def test_the_headline_margin_is_the_lines_margin_before_tax() -> None:
    """20% typed on the line reads as 20.00% at the top, not 20.63%."""
    quote = _quote()
    quote.currency = "USD"
    item = _line("300", "16.008", "5")
    item.cost_rate = Decimal("13.34")
    quote.items.append(item)
    pack = bidpack.build(quote)
    assert pack.landed.total == Decimal("4002.00")
    assert pack.gross_margin == Decimal("800.40")          # 4,802.40 − 4,002.00, before tax
    assert pack.gross_margin_percent == Decimal("20.00")  # on the landed cost
    assert pack.bid_total == Decimal("5042.52")            # the taxed total still leads


def test_the_working_restates_the_totals_in_aed_as_zoho_does() -> None:
    quote = _quote()
    quote.currency, quote.fx_rate, quote.supplier_currency = "USD", Decimal("3.672501"), "AED"
    quote.items.append(_line("300", "16.008", "5"))
    by = {(s.group, s.label): s for s in calculation.steps(quote, bidpack.build(quote))}
    # 4,802.40 × 3.672501 = 17,636.82
    assert by[("base", "Taxable amount in AED")].result == Decimal("17636.82")
    assert by[("base", "Tax in AED")].result == Decimal("881.84")
    assert by[("base", "Total in AED")].result == Decimal("18518.66")


# ── pricing from a supplier ─────────────────────────────────────────────

from app.models.comparison import SupplierQuote, SupplierQuoteItem  # noqa: E402
from app.quoting import bidpack, calculation  # noqa: E402
from app.quoting.fx import FxUnavailableError, rate_between, zoho_rate  # noqa: E402
from app.quoting.service import (  # noqa: E402
    _carry_over,
    _line_from,
    _pricing_rate,
    convert_figures,
)

ZOHO = [
    {"currency_code": "AED", "exchange_rate": 0.0, "is_base_currency": True},
    {"currency_code": "USD", "exchange_rate": 3.672501, "effective_date": "2026-09-01"},
    {"currency_code": "GBP", "exchange_rate": 4.93, "effective_date": "2026-08-15"},
    {"currency_code": "SAR", "exchange_rate": 0.0},
]


def _offer(currency: str, unit_price: str) -> tuple[SupplierQuote, SupplierQuoteItem]:
    quote = SupplierQuote(supplier_name="Deluxe", currency=currency, fx_rate=Decimal(1))
    item = SupplierQuoteItem(
        description="LED panel", quantity=Decimal(300), unit_price=Decimal(unit_price)
    )
    return quote, item


def test_the_rate_reads_the_way_zoho_states_it() -> None:
    usd_aed = rate_between(ZOHO, "USD", "AED")
    assert usd_aed.rate == Decimal("3.672501")           # 1 USD = 3.672501 AED, verbatim
    assert usd_aed.effective_date.isoformat() == "2026-09-01"
    assert rate_between(ZOHO, "AED", "USD").rate == Decimal("0.27229400")
    assert rate_between(ZOHO, "GBP", "USD").rate == (
        Decimal("4.93") / Decimal("3.672501")
    ).quantize(Decimal("0.00000001"))
    assert rate_between(ZOHO, "usd", "usd").rate == Decimal(1)


def test_a_currency_zoho_has_not_priced_is_refused_not_zeroed() -> None:
    import pytest
    with pytest.raises(FxUnavailableError, match="no exchange rate for SAR"):
        rate_between(ZOHO, "AED", "SAR")
    with pytest.raises(FxUnavailableError, match="does not list JPY"):
        rate_between(ZOHO, "JPY", "AED")


def test_an_aed_offer_into_a_usd_quote_lands_on_zohos_number() -> None:
    """AED 49 ÷ 3.672501 = 13.34 → +20% = 16.01 → 300 units = 4,803.00."""
    class Zoho:
        async def currencies(self):
            return ZOHO
    found = asyncio.run(zoho_rate(Zoho(), quote_currency="USD", supplier_currency="AED"))
    request = _quote()
    request.currency, request.fx_rate = "USD", found.rate
    quote, item = _offer("AED", "49")
    line = _line_from(item, quote, Decimal(20), fx=_pricing_rate(request, quote))
    assert line["cost_rate"] == Decimal("13.34")
    assert line["rate"] == Decimal("16.01")
    assert line["rate"] * item.quantity == Decimal("4803.00")


def test_the_same_currency_never_converts_whatever_the_rate_says() -> None:
    request = _quote()
    request.currency, request.fx_rate = "AED", Decimal("3.67")
    quote, item = _offer("AED", "49")
    assert _pricing_rate(request, quote) == Decimal(1)
    assert _line_from(item, quote, Decimal(0), fx=Decimal(1))["rate"] == Decimal("49.00")


def test_switching_currency_converts_every_figure_to_the_cent() -> None:
    """AED → USD at 1 AED = 0.272294 USD. Money moves, percentages do not."""
    quote = _quote(discount=100, shipping=50)
    item = _line("300", "58.80", "5")
    item.cost_rate = Decimal("49")
    quote.items.append(item)

    convert_figures(quote, Decimal("0.27229400"))

    assert item.rate == Decimal("16.01")            # 58.80 × 0.272294 = 16.0109
    assert item.cost_rate == Decimal("13.34")       # 49 × 0.272294 = 13.3424
    assert item.tax_percentage == Decimal("5")      # 5% is 5% in any currency
    assert item.quantity == Decimal(300)
    assert quote.discount == Decimal("27.23")
    assert quote.shipping_charge == Decimal("13.61")
    assert quote.total_excl_tax == Decimal("4789.38")  # 4,803.00 − 27.23 + 13.61


def test_the_bid_total_is_the_taxed_total_once_there_are_lines() -> None:
    quote = _quote()
    quote.currency = "USD"
    quote.items.append(_line("300", "16.01", "5"))
    pack = bidpack.build(quote)
    assert pack.bid_total == quote.total == Decimal("5043.15")
    assert pack.bid_total_is_suggested is False


def test_the_working_ends_on_the_taxed_total_and_shows_every_step() -> None:
    quote = _quote(discount=10)
    quote.currency, quote.fx_rate, quote.supplier_currency = "USD", Decimal("3.672501"), "AED"
    quote.target_markup_percent = Decimal(20)
    item = _line("300", "16.01", "5")
    item.name, item.cost_rate, item.tax_name = "LED panel", Decimal("13.34"), "VAT"
    quote.items.append(item)
    steps = calculation.steps(quote, bidpack.build(quote))
    by = {(s.group, s.label): s for s in steps}
    assert by[("rate", "Exchange rate")].result == Decimal("3.672501")
    assert by[("rate", "Exchange rate")].working.startswith("1 USD = 3.672501 AED")
    assert by[("lines", "LED panel")].working == (
        "cost 13.34 (AED ÷ 3.672501) + 20.00% = 16.01 each, to the cent × 300"
    )
    assert by[("tax", "VAT on LED panel")].working.startswith("5% of 4,803.00")
    assert by[("tax", "Total incl. tax")].result == quote.total
    assert by[("bid", "Bid total")].result == quote.total
    assert [s.group for s in steps] == sorted(
        (s.group for s in steps),
        key=["rate", "lines", "totals", "tax", "base", "landed", "bid"].index,
    )


def test_a_rate_typed_before_the_change_is_shown_as_it_is_stored() -> None:
    quote = _quote()
    item = _line("300", "16.008", None)
    item.name, item.cost_rate = "LED panel", Decimal("13.34")
    quote.items.append(item)
    working = {s.label: s.working for s in calculation.steps(quote, bidpack.build(quote))}
    assert working["LED panel"] == "cost 13.34 + 20.00% = 16.008 each × 300"


def test_choosing_a_supplier_again_keeps_the_tax_typed_on_each_line() -> None:
    """A supplier's document has no VAT per item. Rebuilding the lines from it
    must not quietly un-tax the quote."""
    quote = _quote()
    item = _line("300", "16.008", "5")
    item.name, item.cost_rate = "LED panel", Decimal("13.34")
    item.tax_name, item.position = "VAT", 0
    quote.items.append(item)

    def rebuilt():
        return {"name": "LED panel", "quantity": Decimal(300),
                "cost_rate": Decimal("13.34"), "rate": Decimal("16.01")}

    kept = _carry_over(quote, [rebuilt()])

    assert kept[0]["tax_name"] == "VAT"
    assert kept[0]["tax_percentage"] == Decimal("5")
    assert "tax_name" not in _carry_over(quote, [rebuilt(), rebuilt()])[0]
