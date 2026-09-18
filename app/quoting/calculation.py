"""Every sum on a quote, written out.

A total on a screen is a claim. This is the working behind it — each figure
with the arithmetic that produced it, in the order the arithmetic runs — so a
person checking a bid can see which number they disagree with rather than
which total looks wrong.

Computed from the same objects the totals are computed from, on every read,
so it cannot drift from them. Nothing here is stored.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from app.models.quoting import QuoteRequest
from app.quoting.bidpack import BidPack


@dataclass(frozen=True, slots=True)
class Step:
    #: rate, lines, totals, tax, base, landed, bid — the order they are read in.
    group: str
    label: str
    #: The arithmetic, with the numbers in it.
    working: str
    result: Decimal
    #: Null for a percentage or a rate.
    currency: str | None


def _n(value: Decimal | None, places: int = 2) -> str:
    return f"{(value or Decimal(0)):,.{places}f}"


def _qty(value: Decimal) -> str:
    return _n(value, 0) if value == value.to_integral() else _n(value, 4)


def _price(value: Decimal) -> str:
    """As stored. A rate priced since the change is a whole cent; one typed
    before it may be 16.008, and showing that as 16.01 beside a line total of
    16.008 × 300 makes the working disagree with the number it explains."""
    cents = value.quantize(Decimal("0.01"))
    if value == cents:
        return _n(value, 2)
    # The column holds four places, so 16.008 is stored as 16.0080; the
    # trailing zero is the column's, not the price's.
    return _n(value, 4).rstrip("0")


def steps(request: QuoteRequest, pack: BidPack) -> list[Step]:  # noqa: C901
    cur = request.currency or "AED"
    out: list[Step] = []

    # ── the rate ────────────────────────────────────────────────────
    fx = request.fx_rate if request.fx_rate and request.fx_rate > 0 else None
    foreign = (request.supplier_currency or "").upper()
    converting = bool(fx and foreign and foreign != cur)
    if converting:
        out.append(Step(
            "rate",
            "Exchange rate",
            f"1 {cur} = {fx} {foreign}, as Zoho Books has it. Every {foreign} figure "
            f"below is divided by it.",
            fx,
            None,
        ))

    # ── the lines ───────────────────────────────────────────────────
    for item in request.items:
        qty = item.quantity or Decimal(0)
        rate = item.rate or Decimal(0)
        parts: list[str] = []
        if item.cost_rate is not None and item.cost_rate > 0:
            # The stored cost, not a source figure worked backwards from it:
            # 13.3424 ÷ 0.27229447 is 48.9999, and a working that shows 48.9999
            # for a supplier who quoted 49 is a working nobody trusts.
            parts.append(f"cost {_price(item.cost_rate)}")
            if converting:
                parts.append(f"({foreign} ÷ {fx})")
            # The markup the bid is built at, where one is set; the selling rate
            # is rounded to the cent afterwards, so the implied figure would read
            # 19.99% for a 20% markup.
            if request.target_markup_percent is not None:
                parts.append(
                    f"+ {_n(request.target_markup_percent)}% = {_price(rate)} each, to the cent"
                )
            else:
                markup = (rate - item.cost_rate) / item.cost_rate * Decimal(100)
                parts.append(f"+ {_n(markup)}% = {_price(rate)} each")
        else:
            parts.append(f"{_price(rate)} each")
        parts.append(f"× {_qty(qty)}")
        if item.discount:
            parts.append(f"− {_n(item.discount)} discount")
        out.append(Step("lines", item.name, " ".join(parts), item.line_total, cur))

    # ── the totals ──────────────────────────────────────────────────
    out.append(Step(
        "totals", "Sub-total", f"The {len(request.items)} line(s) above", request.sub_total, cur,
    ))
    if request.discount:
        out.append(Step("totals", "Discount", "Taken off the sub-total", -request.discount, cur))
    if request.shipping_charge:
        out.append(Step("totals", "Shipping", "Added", request.shipping_charge, cur))
    if request.adjustment:
        out.append(Step(
            "totals", "Adjustment", "Added; negative takes money off", request.adjustment, cur,
        ))
    out.append(Step(
        "totals",
        "Total before tax",
        f"{_n(request.sub_total)} − {_n(request.discount)} + {_n(request.shipping_charge)} "
        f"+ {_n(request.adjustment)}",
        request.total_excl_tax,
        cur,
    ))

    # ── the tax ─────────────────────────────────────────────────────
    taxed = [i for i in request.items if i.tax_percentage]
    for item in taxed:
        pct = _n(item.tax_percentage, 3).rstrip("0").rstrip(".")
        out.append(Step(
            "tax",
            f"{item.tax_name or 'Tax'} on {item.name}",
            f"{pct}% of {_n(item.line_total)}, rounded on the line as Zoho does",
            item.tax_amount,
            cur,
        ))
    out.append(Step(
        "tax",
        "Tax",
        "The tax lines above, summed" if taxed else "No tax on any line",
        request.tax_total,
        cur,
    ))
    out.append(Step(
        "tax",
        "Total incl. tax",
        f"{_n(request.total_excl_tax)} + {_n(request.tax_total)}. The figure the customer "
        f"pays, and the one every figure below is measured against",
        request.total,
        cur,
    ))

    # ── in AED, as Zoho's tax summary reports it ───────────────────
    # Zoho's estimate ends with the same three figures restated in the
    # organisation's base currency at the estimate's rate. The quote knows
    # that rate whenever its supplier is in AED — "1 USD = 3.672501 AED" is
    # the same number — so the summary is shown then, and not guessed at
    # otherwise.
    base = "AED"
    if cur != base and converting and foreign == base:
        for label, value in (
            ("Taxable amount", request.total_excl_tax),
            ("Tax", request.tax_total),
            ("Total", request.total),
        ):
            out.append(Step(
                "base",
                f"{label} in {base}",
                f"{cur} {_n(value)} × {fx}, as Zoho's tax summary states it",
                (value * fx).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                base,
            ))

    # ── the landed cost, when there is more to it than the goods ────
    landed = pack.landed
    if len(landed.elements) > 1 or converting:
        for el in landed.elements:
            source_side = (el.source_currency or "").upper()
            if el.amount_source is not None and converting and source_side and source_side != cur:
                working = f"{source_side} {_n(el.amount_source)} ÷ {fx}"
            else:
                working = el.basis or (
                    "Worked out from the rows above" if el.computed else "As entered"
                )
            out.append(Step("landed", el.label, working, el.amount_base, cur))
        out.append(Step(
            "landed",
            "Landed cost",
            "The goods plus everything after them, up to the customer's door",
            landed.total,
            cur,
        ))
        if landed.per_unit is not None and landed.quantity:
            out.append(Step(
                "landed",
                "Landed cost per unit",
                f"{_n(landed.total)} ÷ {_qty(landed.quantity)}",
                landed.per_unit,
                cur,
            ))

    # ── the bid ─────────────────────────────────────────────────────
    out.append(Step("bid", "Bid total", "The total incl. tax, above", pack.bid_total, cur))
    if landed.total:
        out.append(Step(
            "bid",
            "Gross margin",
            f"{_n(request.total_excl_tax if request.items else pack.bid_total)} before tax "
            f"− {_n(landed.total)} landed cost",
            pack.gross_margin,
            cur,
        ))
        if pack.gross_margin_percent is not None:
            out.append(Step(
                "bid",
                "Margin",
                f"{_n(pack.gross_margin)} ÷ {_n(landed.total)} landed cost × 100",
                pack.gross_margin_percent,
                None,
            ))
    return out
