"""Exchange rates, from Zoho Books and nowhere else.

The counterpart to every quote here is an estimate in Zoho Books, and Zoho
converts at the rates in its own currency table. Converting at anything else —
a bank's rate, a remembered peg, a figure in somebody's head — is how one job
came to be priced at 3.66 in one document and 3.6725 in the other, and the two
disagreed by exactly the difference. So the rate is read from Zoho and written
onto the quote, where every figure that follows can see it.

**A rate is stated the way Zoho states it: "1 USD = 3.672501 AED".** One unit
of the quote's currency, in the supplier's. That is the figure a person sees in
Zoho and the figure they will check ours against, so it is stored verbatim —
not inverted to 0.27229400 and rounded on the way, which is what turned
3.672501 into 3.672507 the first time.

Zoho holds one rate per currency, against the organisation's base currency —
AED here. A rate between two other currencies is the ratio of their two rates
to the base. A currency Zoho lists at 0 has never been given a rate, and is
refused rather than treated as worthless.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Protocol


class FxUnavailableError(Exception):
    """Zoho has no usable rate for one side. Safe to show a user."""


class CurrencyTable(Protocol):
    async def currencies(self) -> list[dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class FxQuote:
    quote_currency: str
    supplier_currency: str
    #: Units of ``supplier_currency`` per one unit of ``quote_currency`` —
    #: "1 USD = 3.672501 AED" is ``rate = 3.672501``. This is the shape of
    #: ``QuoteRequest.fx_rate``, so it is written onto a quote unchanged.
    rate: Decimal
    base_currency: str
    #: Zoho's own figures — base units per unit of each side — so the working
    #: can be shown rather than trusted.
    quote_in_base: Decimal
    supplier_in_base: Decimal
    effective_date: date | None
    source: str = "zoho"


#: ``QuoteRequest.fx_rate`` is Numeric(18, 8). Zoho's rates have at most six
#: places, so nothing is lost storing them; the quantize is for ratios.
_PLACES = Decimal("0.00000001")


def rate_between(
    table: list[dict[str, Any]], quote_currency: str, supplier_currency: str
) -> FxQuote:
    """One unit of the quote's currency in the supplier's, from Zoho's table.
    Pure, so it is testable against a table typed by hand."""
    ours, theirs = quote_currency.strip().upper(), supplier_currency.strip().upper()
    rows = {str(row.get("currency_code", "")).upper(): row for row in table}
    base_row = next((row for row in table if row.get("is_base_currency")), None)
    if base_row is None:
        raise FxUnavailableError("Zoho Books did not say which currency is its base.")
    base = str(base_row["currency_code"]).upper()

    def in_base(code: str) -> tuple[Decimal, date | None]:
        if code == base:
            return Decimal(1), None
        row = rows.get(code)
        if row is None:
            raise FxUnavailableError(
                f"Zoho Books does not list {code} as a currency, so there is no "
                f"rate to convert at. Add it there first."
            )
        rate = Decimal(str(row.get("exchange_rate") or 0))
        if rate <= 0:
            raise FxUnavailableError(
                f"Zoho Books has no exchange rate for {code}. Set one under "
                f"Settings → Currencies in Zoho Books and try again."
            )
        return rate, _as_date(row.get("effective_date"))

    quote_in_base, quote_date = in_base(ours)
    supplier_in_base, supplier_date = in_base(theirs)
    rate = (quote_in_base / supplier_in_base).quantize(_PLACES, rounding=ROUND_HALF_UP)
    dates = [d for d in (quote_date, supplier_date) if d is not None]
    return FxQuote(
        quote_currency=ours,
        supplier_currency=theirs,
        rate=rate,
        base_currency=base,
        quote_in_base=quote_in_base,
        supplier_in_base=supplier_in_base,
        effective_date=max(dates) if dates else None,
    )


async def zoho_rate(
    zoho: CurrencyTable, *, quote_currency: str, supplier_currency: str
) -> FxQuote:
    """Zoho's rate, right now. One read of the table and nothing cached: a rate
    a day stale on a bid that stands for months is a rounding error, but a
    rate cached across a change in Zoho is a wrong price."""
    return rate_between(await zoho.currencies(), quote_currency, supplier_currency)


def _as_date(raw: Any) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None
