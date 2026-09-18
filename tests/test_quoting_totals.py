"""The three totals a customer reads, and how tax gets into the last one.

Pure arithmetic on the model — no database, no routes — because that is all it
is, and a four-minute route test to check an addition is the wrong tool.
"""

from __future__ import annotations

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


def test_tax_rounds_once_to_the_cent_not_per_line() -> None:
    quote = _quote()
    # 3 × 0.333... would each round to 0.33; the true sum rounds to 1.00.
    for _ in range(3):
        quote.items.append(_line("1", "6.666666", "5"))
    assert quote.tax_total == Decimal("1.00")


# ── pricing from a supplier ─────────────────────────────────────────────

from app.models.comparison import SupplierQuote, SupplierQuoteItem  # noqa: E402
from app.quoting.service import _line_from, _pricing_rate  # noqa: E402


def _offer(
    currency: str, unit_price: str, fx: str = "1"
) -> tuple[SupplierQuote, SupplierQuoteItem]:
    quote = SupplierQuote(supplier_name="Deluxe", currency=currency, fx_rate=Decimal(fx))
    item = SupplierQuoteItem(
        description="LED panel", quantity=Decimal(300), unit_price=Decimal(unit_price)
    )
    return quote, item


def test_a_foreign_offer_is_priced_at_the_rate_the_bid_is_costed_on() -> None:
    """AED 49 into a USD quote at the bid's own rate — not at the supplier
    quote's, which nobody set and which is therefore 1."""
    request = _quote()
    request.currency = "USD"
    request.fx_rate = Decimal("0.27322")            # 1 / 3.66
    quote, item = _offer("AED", "49")

    line = _line_from(item, quote, Decimal(20), fx=_pricing_rate(request, quote))

    assert line["cost_rate"] == Decimal("13.3878")  # 49 × 0.27322, four places
    assert line["rate"] == Decimal("16.07")         # +20%, to the cent — Zoho's number
    assert line["rate"] * item.quantity == Decimal("4821.00")


def test_without_a_bid_rate_the_supplier_quotes_own_rate_still_applies() -> None:
    request = _quote()
    request.currency = "USD"
    quote, item = _offer("AED", "49", fx="0.2723")
    assert _pricing_rate(request, quote) == Decimal("0.2723")


def test_the_same_currency_never_converts_whatever_the_bid_rate_says() -> None:
    request = _quote()
    request.currency = "AED"
    request.fx_rate = Decimal("0.27")
    quote, item = _offer("AED", "49")
    assert _pricing_rate(request, quote) == Decimal(1)
    assert _line_from(item, quote, Decimal(0), fx=Decimal(1))["rate"] == Decimal("49.00")


# ── Zoho's rate, and the working ────────────────────────────────────────

import asyncio  # noqa: E402

from app.quoting import bidpack, calculation  # noqa: E402
from app.quoting.fx import FxUnavailableError, rate_between, zoho_rate  # noqa: E402

ZOHO = [
    {"currency_code": "AED", "exchange_rate": 0.0, "is_base_currency": True},
    {"currency_code": "USD", "exchange_rate": 3.672501, "effective_date": "2026-09-01"},
    {"currency_code": "GBP", "exchange_rate": 4.93, "effective_date": "2026-08-15"},
    {"currency_code": "SAR", "exchange_rate": 0.0},
]


def test_zohos_table_gives_the_rate_the_estimate_will_use() -> None:
    aed_usd = rate_between(ZOHO, "AED", "USD")
    assert aed_usd.rate == Decimal("0.27229400")        # 1 / 3.672501, to the bid's precision
    assert aed_usd.effective_date.isoformat() == "2026-09-01"
    assert rate_between(ZOHO, "USD", "AED").rate == Decimal("3.672501")
    assert rate_between(ZOHO, "GBP", "USD").rate == (
        Decimal("4.93") / Decimal("3.672501")
    ).quantize(Decimal("0.00000001"))
    assert rate_between(ZOHO, "usd", "usd").rate == Decimal(1)


def test_a_currency_zoho_has_not_priced_is_refused_not_zeroed() -> None:
    import pytest
    with pytest.raises(FxUnavailableError, match="no exchange rate for SAR"):
        rate_between(ZOHO, "SAR", "AED")
    with pytest.raises(FxUnavailableError, match="does not list JPY"):
        rate_between(ZOHO, "JPY", "AED")


def test_the_whole_chain_lands_on_zohos_number() -> None:
    """AED 49 from the supplier → Zoho's rate → +20% → 300 units, in cents."""
    class Zoho:
        async def currencies(self):
            return ZOHO
    found = asyncio.run(zoho_rate(Zoho(), from_currency="AED", to_currency="USD"))
    request = _quote()
    request.currency, request.fx_rate = "USD", found.rate
    quote, item = _offer("AED", "49")
    line = _line_from(item, quote, Decimal(20), fx=_pricing_rate(request, quote))
    assert line["cost_rate"] == Decimal("13.3424")
    assert line["rate"] == Decimal("16.01")
    assert line["rate"] * item.quantity == Decimal("4803.00")


def test_a_rate_typed_before_the_change_is_shown_as_it_is_stored() -> None:
    quote = _quote()
    item = _line("300", "16.008", None)
    item.name, item.cost_rate = "LED panel", Decimal("13.34")
    quote.items.append(item)
    working = {s.label: s.working for s in calculation.steps(quote, bidpack.build(quote))}
    assert working["LED panel"] == "cost 13.3400 + 20.00% = 16.008 each × 300"


def test_the_bid_total_is_the_taxed_total_once_there_are_lines() -> None:
    quote = _quote()
    quote.currency = "USD"
    quote.items.append(_line("300", "16.01", "5"))
    pack = bidpack.build(quote)
    assert pack.bid_total == quote.total == Decimal("5043.15")
    assert pack.bid_total_is_suggested is False


def test_the_working_ends_on_the_taxed_total_and_shows_every_step() -> None:
    quote = _quote(discount=10)
    quote.currency, quote.fx_rate, quote.supplier_currency = "USD", Decimal("0.27229400"), "AED"
    quote.target_markup_percent = Decimal(20)
    item = _line("300", "16.01", "5")
    item.name, item.cost_rate, item.tax_name = "LED panel", Decimal("13.3424"), "VAT"
    quote.items.append(item)
    steps = calculation.steps(quote, bidpack.build(quote))
    by = {(s.group, s.label): s for s in steps}
    assert by[("rate", "1 AED in USD")].result == Decimal("0.27229400")
    assert by[("lines", "LED panel")].working == (
        "cost 13.3424 (AED × 0.27229400) + 20.00% = 16.01 each, to the cent × 300"
    )
    assert by[("tax", "VAT on LED panel")].working == "5% of 4,803.00"
    assert by[("tax", "Total incl. tax")].result == quote.total
    assert by[("bid", "Bid total")].result == quote.total
    assert [s.group for s in steps] == sorted(
        (s.group for s in steps), key=["rate", "lines", "totals", "tax", "landed", "bid"].index
    )


# ── re-pricing what was priced by hand ──────────────────────────────────

from app.quoting.service import implied_markup  # noqa: E402


def test_the_markup_is_read_back_off_hand_priced_lines() -> None:
    quote = _quote()
    item = _line("300", "16.008", "5")
    item.cost_rate = Decimal("13.34")
    quote.items.append(item)
    assert implied_markup(quote) == Decimal("20.00")
    quote.target_markup_percent = Decimal("45")
    assert implied_markup(quote) == Decimal("45")


def test_a_quote_with_no_cost_on_any_line_implies_no_markup() -> None:
    quote = _quote()
    quote.items.append(_line("1", "100", None))
    assert implied_markup(quote) == Decimal(0)


def test_re_pricing_keeps_the_tax_typed_on_each_line() -> None:
    """A supplier's document has no VAT per item. Rebuilding the lines from it
    must not quietly un-tax the quote."""
    from app.quoting.service import _carry_over

    quote = _quote()
    item = _line("300", "16.008", "5")
    item.name, item.cost_rate = "LED panel", Decimal("13.34")
    item.tax_name, item.position = "VAT", 0
    quote.items.append(item)
    def rebuilt():
        return {"name": "LED panel", "quantity": Decimal(300),
                "cost_rate": Decimal("13.3424"), "rate": Decimal("16.01")}

    kept = _carry_over(quote, [rebuilt()])

    assert kept[0]["tax_name"] == "VAT"
    assert kept[0]["tax_percentage"] == Decimal("5")
    # A different supplier with a different number of lines carries nothing.
    assert "tax_name" not in _carry_over(quote, [rebuilt(), rebuilt()])[0]
