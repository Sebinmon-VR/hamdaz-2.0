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
from decimal import Decimal

from app.models.quoting import QuoteRequest
from app.quoting.bidpack import BidPack


@dataclass(frozen=True, slots=True)
class Step:
    #: rate, lines, totals, tax, landed, bid — the order they are read in.
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
            f"1 {foreign} in {cur}",
            f"The bid's exchange rate. Every {foreign} figure below is multiplied by it.",
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
            parts.append(f"cost {_n(item.cost_rate, 4)}")
            if converting:
                parts.append(f"({foreign} × {fx})")
            # The markup the bid is built at, where one is set; the selling rate
            # is rounded to the cent afterwards, so the implied figure would read
            # 19.99% for a 20% markup.
            if request.target_markup_percent is not None:
                parts.append(
                    f"+ {_n(request.target_markup_percent)}% = {_n(rate)} each, to the cent"
                )
            else:
                markup = (rate - item.cost_rate) / item.cost_rate * Decimal(100)
                parts.append(f"+ {_n(markup)}% = {_n(rate)} each")
        else:
            parts.append(f"{_n(rate)} each")
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
            f"{pct}% of {_n(item.line_total)}",
            (item.line_total * item.tax_percentage / Decimal(100)).quantize(Decimal("0.0001")),
            cur,
        ))
    out.append(Step(
        "tax",
        "Tax",
        "The tax lines above, summed and rounded once to the cent"
        if taxed else "No tax on any line",
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

    # ── the landed cost, when there is more to it than the goods ────
    landed = pack.landed
    if len(landed.elements) > 1 or converting:
        for el in landed.elements:
            source_side = (el.source_currency or "").upper()
            if el.amount_source is not None and converting and source_side and source_side != cur:
                working = f"{source_side} {_n(el.amount_source, 4)} × {fx}"
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
            f"{_n(pack.bid_total)} − {_n(landed.total)} landed cost",
            pack.gross_margin,
            cur,
        ))
        if pack.gross_margin_percent is not None:
            out.append(Step(
                "bid",
                "Margin",
                f"{_n(pack.gross_margin)} ÷ {_n(pack.bid_total)} × 100",
                pack.gross_margin_percent,
                None,
            ))
    return out
