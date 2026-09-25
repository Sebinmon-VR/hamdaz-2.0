"""The arithmetic behind a bid: landed cost, the margin ladder, what the buyer sees.

Everything in here is **computed from the stored inputs and never stored**. The
inputs are the supplier's prices, the freight and duty estimates somebody typed,
the FX rate the bid is costed at and the margin the business wants. The outputs
— the CIF value, the duty, the landed cost, the price at each margin, the uplift
the buyer will read off our bid against the principal's quotation — are all
derived here, on read.

That is a deliberate rule and it is worth stating why. A landed cost gets edited:
a forwarder comes back with a real rate, the FX moves, the supplier drops the
certificate charge. If the total were a column, every one of those edits would
have to remember to rewrite it, and the first one that forgot would leave a quote
whose parts and whose total disagree. People believe the total. So there is no
total to believe — only the parts, and this module.

The two structural decisions:

**The goods cost is derived from the priced lines, not held as a cost row.** A
quote already knows what its lines cost — ``cost_rate`` per line, put there when
the supplier was chosen. Holding the same money a second time as a "goods" row in
the build-up would mean repricing from a different supplier silently leaves the
old cost sitting at the top of the landed cost. So the build-up *starts* from the
lines and the stored rows are everything on top: haulage, freight, certificates,
insurance, clearance, the bank.

**Duty and financing are computed, not typed.** Duty is a percentage of the CIF
value, which is itself a sum of other rows; financing is a rate over a number of
days. Both are arithmetic on figures already here, and a typed figure that is
supposed to be arithmetic is just a stale figure waiting to happen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from app.models.quoting import (
    SEVERITY_ORDER,
    CostStage,
    QuoteRequest,
    Severity,
)

#: Money is presented to two places. Rates and per-unit costs are kept to four
#: internally (see ``_RATE`` in the service) and only rounded on the way out.
_MONEY = Decimal("0.01")
_PERCENT = Decimal("0.01")
_ZERO = Decimal(0)

#: Days in the year used for financing. 365 rather than 360: the exposure is
#: counted in calendar days because that is how long the money is actually gone.
_YEAR = Decimal(365)

#: The ladder shown beside whatever margin the bid is actually built at, so the
#: person deciding sees what the neighbouring positions are worth rather than
#: having to ask for each one. Margins, as shares of the selling price — the
#: terms the report's walk-away line is in. The recommended margin is merged
#: in, so a bid at 27% shows 27% in its place in the ladder rather than not at
#: all.
DEFAULT_MARGIN_LADDER: tuple[Decimal, ...] = (
    Decimal(15),
    Decimal(25),
    Decimal(35),
    Decimal(45),
    Decimal(50),
)


def _money(value: Decimal | None) -> Decimal:
    return (value or _ZERO).quantize(_MONEY, rounding=ROUND_HALF_UP)


def _percent(value: Decimal | None) -> Decimal:
    return (value or _ZERO).quantize(_PERCENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class CostElement:
    """One row of the build-up, whether it was typed or worked out.

    ``computed`` is the difference and it is shown, not hidden: a person
    checking a landed cost needs to know which numbers they can argue with and
    which ones follow from the ones above.
    """

    ref: int
    stage: CostStage
    label: str
    basis: str | None
    amount_source: Decimal | None
    source_currency: str | None
    amount_base: Decimal
    is_principal: bool
    is_firm: bool
    computed: bool
    notes: str | None
    #: Present on stored rows only. Null on the derived goods, duty and
    #: financing rows, which have nothing to edit.
    id: str | None = None
    #: Set on a stored row stated as a rate — "insurance, 1% of the goods".
    #: The amount is then worked out here rather than read off the row.
    percent: Decimal | None = None
    percent_of: str | None = None


def _rated(row, base: Decimal) -> Decimal | None:
    """A row's amount when it is a rate over ``base``, else ``None``."""
    if getattr(row, "percent", None) is None:
        return None
    return base * Decimal(str(row.percent)) / Decimal(100)


def _rate_basis(row, what: str) -> str:
    return f"{_percent(Decimal(str(row.percent)))}% of the {what}"


@dataclass(frozen=True, slots=True)
class LandedCost:
    """What it costs to put the goods where the customer wants them."""

    currency: str
    elements: list[CostElement]
    #: Everything up to arrival. This is what duty is charged on.
    cif_subtotal: Decimal
    customs_duty: Decimal
    financing_cost: Decimal
    #: Duty, financing and everything else after the border.
    destination_subtotal: Decimal
    total: Decimal
    #: The quantity the per-unit figures are over, when there is a sensible one.
    quantity: Decimal | None
    per_unit: Decimal | None
    #: Why there is no per-unit figure, when there is not. A bid over several
    #: lines with different units has no meaningful cost "per unit", and an
    #: average of a metre and a pump is worse than a blank.
    per_unit_note: str | None
    #: What share of the cost the supplier or forwarder has actually committed
    #: to. The rest is our estimate, and every point of it that comes in high
    #: comes out of the margin.
    firm_percent: Decimal
    #: The landed cost per unit of goods cost — total ÷ goods, to eight
    #: places; 1 when nothing is costed. A line's landed cost is its cost ×
    #: this: freight, insurance, duty and the bank's cut shared out in
    #: proportion to what each line cost. Selling prices are built on it, so
    #: the margin typed is the margin kept after everything is paid.
    uplift: Decimal
    #: The supplier's own quoted goods value — what the buyer sees if the RFP
    #: makes the principal's quotation a mandatory attachment.
    principal_value: Decimal


@dataclass(frozen=True, slots=True)
class MarkupScenario:
    """The landed cost priced at one margin: ``landed ÷ (1 − margin)``."""

    #: What that adds to the cost, as a share of the cost — the number a buyer
    #: sees when they read our price against the supplier's. A 45% margin is
    #: an 82% markup.
    markup_percent: Decimal
    unit_sell: Decimal | None
    total_sell: Decimal
    #: The rung: the margin kept, as a share of the selling price.
    margin_percent: Decimal
    #: True on the margin the bid is actually being built at.
    is_target: bool


@dataclass(frozen=True, slots=True)
class Disclosure:
    """What the buyer will make of our price if they see the supplier's.

    On tenders that make the principal's quotation a mandatory attachment, the
    buyer reads our price against what we paid, and the difference looks like
    margin. Most of it is not — it is freight, duty, documentation and the cost
    of paying a supplier five months before anybody pays us. This is the number
    that has to be anticipated and answered with a cost breakdown, because the
    alternative is answering it live in a clarification.
    """

    principal_value: Decimal
    bid_value: Decimal
    #: How much bigger our price looks than the principal's, as a percentage.
    apparent_uplift_percent: Decimal | None
    #: The part of that which is genuine landed cost rather than margin.
    recoverable_cost: Decimal
    #: What we actually keep, as a share of the bid.
    true_margin_percent: Decimal | None
    #: Whether the RFP obliges us to hand the principal's quotation over.
    disclosed: bool


@dataclass(frozen=True, slots=True)
class RedFlag:
    """A compliance row urgent enough to be read before the bid is sent."""

    id: str
    ref: str | None
    severity: Severity
    status: str
    issue: str
    action: str | None
    owner: str | None
    resolved: bool


@dataclass(frozen=True, slots=True)
class BidPack:
    """Everything derived, in the order a person reads it."""

    landed: LandedCost
    scenarios: list[MarkupScenario]
    #: The scenario at the bid's own margin, when one is set.
    target: MarkupScenario | None
    #: What is actually going in — the rounded price somebody decided, falling
    #: back to the target scenario when nobody has rounded anything yet.
    bid_unit_price: Decimal | None
    bid_total: Decimal
    #: True when the bid total is the target scenario rather than a decision.
    bid_total_is_suggested: bool
    gross_margin: Decimal
    gross_margin_percent: Decimal | None
    disclosure: Disclosure
    red_flags: list[RedFlag]
    #: Unresolved compliance rows that should stop a submission, and mandatory
    #: portal fields with nothing in them. Advisory — it is said loudly and it
    #: does not take the button away, because the person looking at the bid on
    #: the afternoon of the deadline knows things this does not.
    warnings: list[str] = field(default_factory=list)


# ── the build-up ───────────────────────────────────────────────────────


def goods_cost(request: QuoteRequest) -> Decimal:
    """What the priced lines cost us, before anything is moved.

    Taken from the lines rather than from a cost row, so that repricing from a
    different supplier moves the landed cost with it. A line with no cost — a
    rate somebody typed by hand — contributes nothing here, which is honest:
    we do not know what it costs.
    """
    return sum(
        ((i.cost_rate or _ZERO) * (i.quantity or _ZERO) for i in request.items),
        _ZERO,
    )


def _bid_quantity(request: QuoteRequest) -> tuple[Decimal | None, str | None]:
    """The quantity a per-unit figure would be over, if one means anything.

    One line, one answer. Several lines and there is no such thing as the cost
    per unit — a bid for forty metres of cable and two pumps has a cost per
    metre and a cost per pump, and the average of them is a number with no
    referent. Tenders are very often a single line, which is why this is worth
    computing at all.
    """
    priced = [i for i in request.items if (i.quantity or _ZERO) > 0]
    if len(priced) == 1:
        return priced[0].quantity, None
    if not priced:
        return None, "No priced lines yet, so there is nothing to divide by."
    units = {(i.unit or "").strip().lower() for i in priced}
    if len(units) == 1 and "" not in units:
        return (
            sum((i.quantity or _ZERO for i in priced), _ZERO),
            None,
        )
    return None, (
        f"{len(priced)} lines in different units — a blended cost per unit "
        f"would not refer to anything. The totals below are for the whole bid."
    )


def landed_cost(request: QuoteRequest) -> LandedCost:
    """The whole build-up, derived rows and stored rows in reading order."""
    currency = request.currency or "AED"
    fx = request.fx_rate if request.fx_rate and request.fx_rate > 0 else None
    goods = goods_cost(request)

    elements: list[CostElement] = []
    ref = 0

    # The goods, from the priced lines. First because it is the thing being
    # bought; everything after it is the cost of having bought it there.
    ref += 1
    elements.append(
        CostElement(
            ref=ref,
            stage=CostStage.ORIGIN,
            label="Goods, as priced from the supplier",
            basis="The quote's own lines",
            # 1 USD = 3.672501 AED: our goods figure, in their currency.
            amount_source=(goods * fx).quantize(Decimal("0.01")) if fx else None,
            source_currency=request.supplier_currency if fx else None,
            amount_base=_money(goods),
            is_principal=True,
            is_firm=request.selected_supplier_quote_id is not None,
            computed=True,
            notes=None,
        )
    )

    stored = sorted(request.cost_lines, key=lambda c: c.position or 0)
    # Partitioned so that every row lands on one side or the other. Testing for
    # destination and treating everything else as origin — rather than testing
    # for each — means a row whose stage is unset cannot fall out of the
    # build-up altogether, which is the worst of the available failures: the
    # total quietly comes out low and nothing says a row is missing. A stage is
    # unset whenever the row was added in this request and the column default
    # has not been applied yet, because that happens at insert.
    destination = [c for c in stored if c.stage == CostStage.DESTINATION]
    origin = [c for c in stored if c.stage != CostStage.DESTINATION]

    # A row stated as a rate is worked out on the goods: everything before
    # arrival is charged on what was bought, and the CIF value is not known
    # until these rows are summed.
    origin_amounts = {
        id(row): (
            rated if (rated := _rated(row, goods)) is not None else (row.amount_base or _ZERO)
        )
        for row in origin
    }

    for row in origin:
        ref += 1
        rated = getattr(row, "percent", None) is not None
        elements.append(
            CostElement(
                ref=ref,
                # The list it came from, not the column — see the partition
                # above. A row reporting a stage it was not summed under would
                # put the customs boundary in the wrong place.
                stage=CostStage.ORIGIN,
                label=row.label,
                basis=_rate_basis(row, "supplier price") if rated else row.basis,
                amount_source=None if rated else row.amount_source,
                source_currency=None if rated else row.source_currency,
                amount_base=_money(origin_amounts[id(row)]),
                # Coerced rather than passed through. A column default is
                # applied by the database at insert, so a row added in this
                # session and not yet flushed reads ``None`` here — and the
                # response for the request that added it is built before the
                # commit. A flag that is None is not a flag.
                is_principal=bool(row.is_principal),
                is_firm=bool(row.is_firm),
                computed=False,
                notes=row.notes,
                id=str(row.id),
                percent=row.percent if rated else None,
                percent_of="goods" if rated else None,
            )
        )

    cif = goods + sum(origin_amounts.values(), _ZERO)

    # Duty, on the CIF value, because that is what customs charges it on.
    duty = cif * (request.customs_duty_percent or _ZERO) / Decimal(100)
    if duty > 0:
        ref += 1
        elements.append(
            CostElement(
                ref=ref,
                stage=CostStage.DESTINATION,
                label="Import duty",
                basis=f"{_percent(request.customs_duty_percent)}% of the CIF value",
                amount_source=None,
                source_currency=None,
                amount_base=_money(duty),
                is_principal=False,
                is_firm=False,
                computed=True,
                notes=None,
            )
        )

    # The cost of the money, while it is out of the door. The base is the CIF
    # value: on the terms that make this bite at all — a supplier wanting paying
    # before manufacture — what goes out is the goods and everything spent
    # getting them moving, and it goes out long before anybody pays us.
    financing = (
        cif
        * (request.financing_rate_percent or _ZERO)
        / Decimal(100)
        * Decimal(request.cash_exposure_days or 0)
        / _YEAR
    )
    if financing > 0:
        ref += 1
        elements.append(
            CostElement(
                ref=ref,
                stage=CostStage.DESTINATION,
                label="Cost of financing the pre-payment",
                basis=(
                    f"{_percent(request.financing_rate_percent)}% a year over "
                    f"{request.cash_exposure_days} days"
                ),
                amount_source=None,
                source_currency=None,
                amount_base=_money(financing),
                is_principal=False,
                is_firm=False,
                computed=True,
                notes=None,
            )
        )

    # After arrival a rate may be over the goods or over the CIF value; the
    # bank's cut is on what was paid, a clearance agent's on what arrived.
    destination_amounts = {}
    for row in destination:
        on_cif = (getattr(row, "percent_of", None) or "goods") == "cif"
        rated_amount = _rated(row, cif if on_cif else goods)
        destination_amounts[id(row)] = (
            rated_amount if rated_amount is not None else (row.amount_base or _ZERO)
        )

    for row in destination:
        ref += 1
        rated = getattr(row, "percent", None) is not None
        on_cif = (getattr(row, "percent_of", None) or "goods") == "cif"
        elements.append(
            CostElement(
                ref=ref,
                stage=CostStage.DESTINATION,
                label=row.label,
                basis=(
                    _rate_basis(row, "CIF value" if on_cif else "supplier price")
                    if rated
                    else row.basis
                ),
                amount_source=None if rated else row.amount_source,
                source_currency=None if rated else row.source_currency,
                amount_base=_money(destination_amounts[id(row)]),
                is_principal=bool(row.is_principal),
                is_firm=bool(row.is_firm),
                computed=False,
                notes=row.notes,
                id=str(row.id),
                percent=row.percent if rated else None,
                percent_of=("cif" if on_cif else "goods") if rated else None,
            )
        )

    destination_total = duty + financing + sum(destination_amounts.values(), _ZERO)
    total = cif + destination_total

    quantity, note = _bid_quantity(request)
    per_unit = (
        (total / quantity).quantize(Decimal("0.0001"))
        if quantity and quantity > 0
        else None
    )

    firm = sum((e.amount_base for e in elements if e.is_firm), _ZERO)
    principal = sum((e.amount_base for e in elements if e.is_principal), _ZERO)

    return LandedCost(
        currency=currency,
        elements=elements,
        cif_subtotal=_money(cif),
        customs_duty=_money(duty),
        financing_cost=_money(financing),
        destination_subtotal=_money(destination_total),
        total=_money(total),
        quantity=quantity,
        per_unit=per_unit,
        per_unit_note=note,
        firm_percent=_percent(firm / total * Decimal(100)) if total > 0 else _ZERO,
        uplift=(
            (total / goods).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
            if goods > 0 and total > 0
            else Decimal(1)
        ),
        principal_value=_money(principal),
    )


# ── what to charge for it ──────────────────────────────────────────────


def _scenario(landed: LandedCost, margin: Decimal, *, target: bool) -> MarkupScenario:
    """The landed cost priced to keep ``margin`` percent of the price.

    Selling price = cost ÷ (1 − margin): a margin is a share of what the
    customer pays, so a 20% margin on 100 is 125, not 120. A margin of 100%
    or more has no price; the ladder never holds one, and a bid typed at one
    is refused before it gets here, so it is simply priced at cost.
    """
    share = Decimal(1) - margin / Decimal(100)
    factor = Decimal(1) / share if share > 0 else Decimal(1)
    total = landed.total * factor
    return MarkupScenario(
        # What was added, as a share of the cost. A buyer who is shown the
        # supplier's quotation reads our price against it and sees this
        # number, so it is stated rather than left to be worked out wrong.
        markup_percent=(
            _percent((total - landed.total) / landed.total * Decimal(100))
            if landed.total > 0
            else _ZERO
        ),
        unit_sell=(
            (landed.per_unit * factor).quantize(_MONEY, rounding=ROUND_HALF_UP)
            if landed.per_unit is not None
            else None
        ),
        total_sell=_money(total),
        margin_percent=_percent(margin),
        is_target=target,
    )


def scenarios(landed: LandedCost, target_margin: Decimal | None) -> list[MarkupScenario]:
    """The ladder, with the bid's own margin merged into its place."""
    rungs = {_percent(m) for m in DEFAULT_MARGIN_LADDER}
    if target_margin is not None and target_margin < Decimal(100):
        rungs.add(_percent(target_margin))
    wanted = _percent(target_margin) if target_margin is not None else None
    return [
        _scenario(landed, margin, target=margin == wanted) for margin in sorted(rungs)
    ]


def disclosure(
    landed: LandedCost, bid_total: Decimal, *, disclosed: bool
) -> Disclosure:
    principal = landed.principal_value
    return Disclosure(
        principal_value=principal,
        bid_value=_money(bid_total),
        apparent_uplift_percent=(
            _percent((bid_total - principal) / principal * Decimal(100))
            if principal > 0
            else None
        ),
        # What the uplift is made of, other than margin: everything in the
        # landed cost that is not the principal's own price.
        recoverable_cost=_money(landed.total - principal),
        true_margin_percent=(
            _percent((bid_total - landed.total) / bid_total * Decimal(100))
            if bid_total > 0
            else None
        ),
        disclosed=disclosed,
    )


# ── what still has to be dealt with ────────────────────────────────────


def red_flags(request: QuoteRequest) -> list[RedFlag]:
    """The compliance rows carrying a severity, worst first.

    Derived from the matrix rather than kept as a second list beside it. Two
    lists of the same problems disagree within a week, and then the question
    "have the red flags been cleared" has two answers.
    """
    flagged = [c for c in request.compliance if c.severity]
    flagged.sort(
        key=lambda c: (
            c.resolved_at is not None,
            SEVERITY_ORDER.get(str(c.severity), 99),
            c.position,
        )
    )
    return [
        RedFlag(
            id=str(c.id),
            ref=c.ref,
            severity=c.severity,  # type: ignore[arg-type]
            status=str(c.status),
            issue=c.requirement,
            action=c.action,
            owner=c.owner,
            resolved=c.resolved_at is not None,
        )
        for c in flagged
    ]


def warnings(request: QuoteRequest) -> list[str]:
    """What a person should be told before this goes anywhere.

    Deliberately advisory. Nothing here takes a button away: bids are sent on
    the afternoon of the deadline by people who know things this does not, and
    a system that refuses at four o'clock has not prevented a bad bid, it has
    prevented a bid. Saying it plainly and leaving the decision alone is the
    honest arrangement.
    """
    out: list[str] = []

    stoppers = [c for c in request.compliance if c.is_open and c.severity == Severity.STOPPER]
    if stoppers:
        out.append(
            f"{len(stoppers)} unresolved "
            + ("issue is" if len(stoppers) == 1 else "issues are")
            + " marked as stopping the submission: "
            + ", ".join(f"{c.ref or c.requirement[:40]}" for c in stoppers[:4])
            + ("…" if len(stoppers) > 4 else "")
            + "."
        )

    blocking = [c for c in request.compliance if c.is_blocking and c.severity != Severity.STOPPER]
    if blocking:
        out.append(
            f"{len(blocking)} compliance "
            + ("row is" if len(blocking) == 1 else "rows are")
            + " still non-compliant, open or awaiting a clarification."
        )

    missing = [
        f for f in request.submission_fields if f.is_mandatory and not (f.value or "").strip()
    ]
    if missing:
        out.append(
            f"{len(missing)} mandatory portal "
            + ("field has" if len(missing) == 1 else "fields have")
            + " no value yet: "
            + ", ".join(f.label for f in missing[:4])
            + ("…" if len(missing) > 4 else "")
            + "."
        )

    if request.discloses_principal_price and request.selected_supplier_quote_id:
        out.append(
            "The principal's own quotation is a mandatory attachment, so the buyer "
            "will see what we paid. Submit the cost breakdown alongside it."
        )

    if request.bid_validity_days and request.expiry_date is None:
        out.append(
            f"The bid is offered for {request.bid_validity_days} days but no expiry "
            f"date is set, so nothing checks the supplier's own validity against it."
        )

    return out


# ── the whole thing ────────────────────────────────────────────────────


def build(request: QuoteRequest) -> BidPack:
    """Everything derived from this quote's bid inputs, in one pass."""
    landed = landed_cost(request)
    ladder = scenarios(landed, request.target_markup_percent)
    target = next((s for s in ladder if s.is_target), None)

    # What is actually going in. A rounded price somebody decided beats an
    # arithmetic one, and the two are distinguished so a screen can say which
    # it is showing rather than presenting a suggestion as a decision.
    suggested = target.total_sell if target else _money(landed.total)
    unit = request.submission_unit_price
    if request.items:
        # The priced document itself, tax included. Once a quote has lines,
        # that is the bid — the ladder below is a suggestion for one that has
        # none yet, and a bid total that disagreed with the quote's own total
        # was two numbers for one price.
        bid_total = _money(request.total)
        suggested_only = False
    elif request.submission_total is not None:
        bid_total = _money(request.submission_total)
        suggested_only = False
    elif unit is not None and landed.quantity:
        bid_total = _money(unit * landed.quantity)
        suggested_only = False
    else:
        bid_total = suggested
        suggested_only = True

    if unit is None and landed.quantity and landed.quantity > 0:
        unit = (bid_total / landed.quantity).quantize(_MONEY, rounding=ROUND_HALF_UP)

    # The margin, measured the way the lines are priced: what is made over the
    # landed cost, as a share of the selling price, before tax. A quote priced
    # at a 20% margin on every line, with nothing landed beyond the goods,
    # reads as 20% here. It was measured on the taxed selling price once, so
    # the same quote read 20.63% — which is neither what anyone typed nor a
    # figure anyone could reconcile. Tax is collected, not earned.
    sale = _money(request.total_excl_tax) if request.items else bid_total
    margin = sale - landed.total
    return BidPack(
        landed=landed,
        scenarios=ladder,
        target=target,
        bid_unit_price=unit,
        bid_total=bid_total,
        bid_total_is_suggested=suggested_only,
        gross_margin=_money(margin),
        gross_margin_percent=(
            _percent(margin / sale * Decimal(100)) if landed.total > 0 and sale > 0 else None
        ),
        disclosure=disclosure(
            landed, bid_total, disclosed=request.discloses_principal_price
        ),
        red_flags=red_flags(request),
        warnings=warnings(request),
    )
