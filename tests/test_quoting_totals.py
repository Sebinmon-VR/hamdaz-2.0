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
