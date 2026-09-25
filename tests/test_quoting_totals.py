"""The three totals a customer reads, and how tax gets into the last one.

The tax is one rate on the quote, applied once to the total before tax —
after the discount, the shipping and the adjustment — and rounded once. Not
per line, and not rounded per line.

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
        tax_name="VAT" if "tax" in money else None,
        tax_percentage=Decimal(money["tax"]) if "tax" in money else None,
        items=[],
    )


def _line(qty: str, rate: str, discount: str = "0") -> QuoteRequestItem:
    return QuoteRequestItem(
        name="x",
        quantity=Decimal(qty),
        rate=Decimal(rate),
        discount=Decimal(discount),
    )


def test_tax_is_added_on_top_of_the_total_and_shown_on_its_own() -> None:
    """On the total: the discount comes off and the shipping goes on before
    the 5% is applied, because the tax is on what the customer pays."""
    quote = _quote(discount=100, shipping=50, tax=5)
    quote.items.append(_line("20", "4553"))   # 91,060
    quote.items.append(_line("1", "100"))

    assert quote.sub_total == Decimal("91160")
    assert quote.total_excl_tax == Decimal("91110")     # less 100, plus 50
    assert quote.tax_total == Decimal("4555.50")        # 5% of 91,110
    assert quote.total == Decimal("95665.50")           # excl. tax + tax


def test_a_quote_with_no_tax_totals_exactly_as_before() -> None:
    quote = _quote(discount=1000, shipping=250)
    quote.items.append(_line("2", "12000"))
    quote.items.append(_line("2", "4000"))

    assert quote.tax_total == Decimal("0.00")
    assert quote.total_excl_tax == quote.total == Decimal(32000 - 1000 + 250)


def test_tax_is_worked_out_once_on_the_total_not_line_by_line() -> None:
    """Three lines of 6.666666 at 5%: rounded per line that is 0.33 × 3 =
    0.99; on the total it is 5% of 19.999998 = 1.00. The tax is on the
    total, so the customer pays 1.00."""
    quote = _quote(tax=5)
    for _ in range(3):
        quote.items.append(_line("1", "6.666666"))
    assert quote.tax_total == Decimal("1.00")


def test_a_quote_with_a_zero_rate_carries_no_tax() -> None:
    quote = _quote(tax=0)
    quote.items.append(_line("2", "100"))
    assert quote.tax_total == Decimal("0.00")
    assert quote.total == quote.total_excl_tax == Decimal(200)


def test_a_line_is_its_taxable_amount() -> None:
    item = _line("300", "16.008")
    assert item.line_total == Decimal("4802.4")


def test_the_headline_margin_is_the_lines_margin_before_tax() -> None:
    """The margin on the price before tax, not on the price with it: the
    same quote read 20.63% when it was measured on the taxed total."""
    quote = _quote(tax=5)
    quote.currency = "USD"
    item = _line("300", "16.008")
    item.cost_rate = Decimal("13.34")
    quote.items.append(item)
    pack = bidpack.build(quote)
    assert pack.landed.total == Decimal("4002.00")
    assert pack.gross_margin == Decimal("800.40")          # 4,802.40 − 4,002.00, before tax
    assert pack.gross_margin_percent == Decimal("16.67")  # 800.40 ÷ 4,802.40, of the price
    assert pack.bid_total == Decimal("5042.52")            # the taxed total still leads


def test_the_working_restates_the_totals_in_aed_as_zoho_does() -> None:
    quote = _quote(tax=5)
    quote.currency, quote.fx_rate, quote.supplier_currency = "USD", Decimal("3.672501"), "AED"
    quote.items.append(_line("300", "16.008"))
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
    convert_currency,
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
    """Zoho's order: AED 49 at a 20% margin is 49 ÷ 0.8 = 61.25, is 61 (a
    whole dirham, as Zoho's item price is), ÷ 3.672501 = USD 16.61 → 300
    units = 4,983.00, and 5% on that total is 249.15."""
    class Zoho:
        async def currencies(self):
            return ZOHO
    found = asyncio.run(zoho_rate(Zoho(), quote_currency="USD", supplier_currency="AED"))
    request = _quote(tax=5)
    request.currency, request.fx_rate = "USD", found.rate
    quote, item = _offer("AED", "49")
    line = _line_from(item, quote, Decimal(20), fx=_pricing_rate(request, quote))
    assert line["cost_rate"] == Decimal("13.34")           # 49 ÷ 3.672501, to the cent
    assert line["rate"] == Decimal("16.61")                # 61 ÷ 3.672501, to the cent
    assert line["rate"] * item.quantity == Decimal("4983.00")
    request.items.append(_line(str(item.quantity), str(line["rate"])))
    assert request.total_excl_tax == Decimal("4983.00")
    assert request.tax_total == Decimal("249.15")
    assert request.total == Decimal("5232.15")


def test_a_non_aed_supplier_is_rounded_to_the_cent_not_the_unit() -> None:
    """The whole-unit rounding is Zoho's AED practice, not a general rule."""
    request = _quote()
    request.currency, request.fx_rate = "GBP", Decimal("0.74")   # 1 GBP = 0.74 USD, say
    quote, item = _offer("USD", "10")
    line = _line_from(item, quote, Decimal(25), fx=_pricing_rate(request, quote))
    assert line["rate"] == Decimal("18.01")                # 10 ÷ 0.75 = 13.33, ÷ 0.74
    assert line["cost_rate"] == Decimal("13.51")


def test_the_same_currency_never_converts_whatever_the_rate_says() -> None:
    request = _quote()
    request.currency, request.fx_rate = "AED", Decimal("3.67")
    quote, item = _offer("AED", "49")
    assert _pricing_rate(request, quote) == Decimal(1)
    assert _line_from(item, quote, Decimal(0), fx=Decimal(1))["rate"] == Decimal("49.00")
    # And an AED quote from an AED supplier prices to the dirham, as Zoho does:
    # 49 ÷ 0.8 = 61.25 → 61.
    assert _line_from(item, quote, Decimal(20), fx=Decimal(1))["rate"] == Decimal("61.00")


def test_switching_currency_rounds_an_aed_selling_price_before_converting() -> None:
    """Zoho turns AED 58.80 into its AED 59 item price before converting it."""
    quote = _quote(discount=100, shipping=50, tax=5)
    quote.currency = quote.supplier_currency = "AED"
    item = _line("300", "58.80")
    item.cost_rate = Decimal("49")
    quote.items.append(item)

    class Session:
        async def flush(self) -> None:
            pass

    async def rates(ours: str, theirs: str):
        return rate_between(ZOHO, ours, theirs)

    asyncio.run(convert_currency(Session(), quote, to_currency="USD", rates=rates))

    assert item.rate == Decimal("16.07")            # AED 58.80 → 59; 59 × 0.272294 = 16.0653
    assert item.cost_rate == Decimal("13.34")       # 49 × 0.272294 = 13.3424
    assert quote.tax_percentage == Decimal("5")     # 5% is 5% in any currency
    assert item.quantity == Decimal(300)
    assert quote.discount == Decimal("27.23")
    assert quote.shipping_charge == Decimal("13.61")
    assert quote.total_excl_tax == Decimal("4807.38")  # 4,821.00 − 27.23 + 13.61
    assert quote.tax_total == Decimal("240.37")        # 5% of 4,807.38, once
    assert quote.currency == "USD"
    assert quote.fx_rate == Decimal("3.672501")


def test_the_bid_total_is_the_taxed_total_once_there_are_lines() -> None:
    quote = _quote(tax=5)
    quote.currency = "USD"
    quote.items.append(_line("300", "16.01"))
    pack = bidpack.build(quote)
    assert pack.bid_total == quote.total == Decimal("5043.15")
    assert pack.bid_total_is_suggested is False


def test_the_working_ends_on_the_taxed_total_and_shows_every_step() -> None:
    quote = _quote(discount=10, tax=5)
    quote.currency, quote.fx_rate, quote.supplier_currency = "USD", Decimal("3.672501"), "AED"
    quote.target_markup_percent = Decimal(20)
    # 13.34 ÷ 0.8 = 16.675, to the cent.
    item = _line("300", "16.68")
    item.name, item.cost_rate, item.tax_name = "LED panel", Decimal("13.34"), "VAT"
    quote.items.append(item)
    steps = calculation.steps(quote, bidpack.build(quote))
    by = {(s.group, s.label): s for s in steps}
    assert by[("rate", "Exchange rate")].result == Decimal("3.672501")
    assert by[("rate", "Exchange rate")].working.startswith("1 USD = 3.672501 AED")
    assert by[("lines", "LED panel")].working == (
        "cost 13.34 (AED ÷ 3.672501) ÷ (1 − 20.00% margin) = 16.68 each, to the cent × 300"
    )
    # 5,004.00 less the 10 discount, then the 5%: on the total, once.
    assert by[("tax", "VAT 5%")].working.startswith("5% of 4,994.00 (the total before tax)")
    assert by[("tax", "VAT 5%")].result == Decimal("249.70")
    assert by[("bid", "Margin")].working.startswith("992.00 ÷ 4,994.00 selling price")
    assert by[("tax", "Total incl. tax")].result == quote.total
    assert by[("bid", "Bid total")].result == quote.total
    assert [s.group for s in steps] == sorted(
        (s.group for s in steps),
        key=["rate", "lines", "totals", "tax", "base", "landed", "bid"].index,
    )


def test_a_rate_typed_before_the_change_is_shown_as_it_is_stored() -> None:
    quote = _quote()
    item = _line("300", "16.008")
    item.name, item.cost_rate = "LED panel", Decimal("13.34")
    quote.items.append(item)
    working = {s.label: s.working for s in calculation.steps(quote, bidpack.build(quote))}
    # No margin set on the bid, so the one the price implies is shown.
    assert working["LED panel"] == "cost 13.34 ÷ (1 − 16.67% margin) = 16.008 each × 300"


def test_choosing_a_supplier_again_keeps_the_discount_typed_on_each_line() -> None:
    """A supplier's document knows nothing of the discount typed on a line.
    Rebuilding the lines from it must not quietly drop it. The tax is on the
    quote, so a re-price never touches it."""
    quote = _quote(tax=5)
    item = _line("300", "16.008", discount="12")
    item.name, item.cost_rate, item.position = "LED panel", Decimal("13.34"), 0
    quote.items.append(item)

    def rebuilt():
        return {"name": "LED panel", "quantity": Decimal(300),
                "cost_rate": Decimal("13.34"), "rate": Decimal("16.01")}

    kept = _carry_over(quote, [rebuilt()])

    assert kept[0]["discount"] == Decimal("12")
    assert "discount" not in _carry_over(quote, [rebuilt(), rebuilt()])[0]
    assert quote.tax_percentage == Decimal(5)

# ── the margin rule ────────────────────────────────────────────────────


def test_a_selling_price_keeps_the_margin_as_a_share_of_itself() -> None:
    """Selling price = cost ÷ (1 − margin). 100 at 20% is 125 — a fifth of
    125 is the 25 that was added — not the 120 a markup would give."""
    from app.quoting.service import sell_at

    assert sell_at(Decimal(100), Decimal(20)) == Decimal(125)
    assert sell_at(Decimal(100), Decimal(0)) == Decimal(100)
    assert sell_at(Decimal(49), Decimal(20)) == Decimal("61.25")
    assert sell_at(Decimal(637), Decimal("62.30")).quantize(Decimal("0.01")) == Decimal("1689.66")


def test_a_margin_of_the_whole_price_has_no_price() -> None:
    from app.quoting.service import QuoteError, sell_at

    for impossible in (Decimal(100), Decimal(150)):
        try:
            sell_at(Decimal(100), impossible)
        except QuoteError as exc:
            assert "under 100%" in str(exc)
        else:
            raise AssertionError("a 100% margin was priced")


def test_the_margin_already_on_a_quote_is_read_off_the_price() -> None:
    """So an approver's re-price at the same margin gives the same margin."""
    from app.quoting.service import margin_of

    quote = _quote()
    item = _line("2", "1250")
    item.cost_rate = Decimal(1000)
    quote.items.append(item)

    assert margin_of(quote) == Decimal("20.00")

def test_the_margin_typed_is_the_margin_kept_after_the_landed_cost() -> None:
    """Goods 1,000 with 100 of freight on top: landed 1,100. Priced at 20%,
    the line sells for 1,100 ÷ 0.8 = 1,375 — not 1,000 ÷ 0.8 = 1,250, which
    would keep only 10.9% once the freight is paid."""
    from app.models.quoting import CostStage, QuoteCostLine
    from app.quoting.service import reprice_at_margin

    quote = _quote()
    quote.currency = "USD"
    item = _line("1", "0")
    item.cost_rate = Decimal(1000)
    quote.items.append(item)
    quote.cost_lines = [
        QuoteCostLine(position=1, stage=CostStage.ORIGIN, label="Freight",
                      amount_base=Decimal(100), is_firm=True)
    ]

    assert reprice_at_margin(quote, Decimal(20)) == 1

    assert item.rate == Decimal("1375.00")
    assert quote.target_markup_percent == Decimal(20)
    pack = bidpack.build(quote)
    assert pack.landed.uplift == Decimal("1.10000000")
    assert pack.landed.total == Decimal("1100.00")
    assert pack.gross_margin_percent == Decimal("20.00")
    working = {s.label: s.working for s in calculation.steps(quote, pack)}
    assert working["x"] == (
        "cost 1,000.00 × 1.1000 (landed cost ÷ goods) = 1,100.00 landed "
        "÷ (1 − 20.00% margin) = 1,375.00 each, to the cent × 1"
    )


def test_with_nothing_landed_the_price_is_the_cost_over_the_margin() -> None:
    from app.quoting.service import reprice_at_margin

    quote = _quote()
    quote.currency = "AED"
    item = _line("2", "0")
    item.cost_rate = Decimal(49)
    quote.items.append(item)

    reprice_at_margin(quote, Decimal(20))

    assert item.rate == Decimal(61)                    # 61.25, to the dirham
    assert bidpack.build(quote).landed.uplift == Decimal(1)
