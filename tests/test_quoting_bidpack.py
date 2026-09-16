"""The bid arithmetic: landed cost, the margin ladder, what the buyer sees.

No database. Every one of these is a function of a request object and its rows,
which is the whole point of ``app.quoting.bidpack`` being a module rather than a
handful of properties — the sums that decide what we bid can be attacked
directly, at the speed of a unit test, instead of through a fixture and a
session.

The first case is the one that matters most. It is a real bid: an ADNOC RFP for
twenty welding blankets, quoted EXW Telford in sterling by the OEM, landed DAP
Mussafah. Every figure asserted here comes from the costing workbook a person
built for it by hand, to the cent. If this module and that workbook ever
disagree, one of them is wrong about a real bid, and that is worth a test that
names actual numbers rather than a tolerance.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.models.comparison import SupplierQuote, SupplierQuoteItem
from app.models.quoting import (
    ComplianceStatus,
    CostStage,
    QuoteComplianceItem,
    QuoteCostLine,
    QuoteRequest,
    QuoteRequestItem,
    QuoteSubmissionField,
    Severity,
)
from app.quoting import bidpack, service
from app.quoting.schemas import BidPackOut

#: AED per GBP, as the bid was costed: mid-market plus a spread, because the
#: price stands for ninety days and the money moves once, at the end.
FX = Decimal("4.95")


def request_with(**overrides) -> QuoteRequest:
    """A bare request with its collections loaded, ready to have rows put on it."""
    # The column defaults are applied by the database, and nothing here has
    # been near one — so they are set explicitly, under whatever the caller
    # asked for.
    fields = {
        "title": "test",
        "customer_name": "test",
        "currency": "AED",
        "customs_duty_percent": Decimal(0),
        "financing_rate_percent": Decimal(0),
        "cash_exposure_days": 0,
        "discloses_principal_price": False,
        "multiple_supplier_quotes": False,
        **overrides,
    }
    request = QuoteRequest(**fields)
    request.items = []
    request.cost_lines = []
    request.compliance = []
    request.submission_fields = []
    return request


@pytest.fixture
def cloth() -> QuoteRequest:
    """RFP 6000149233 — twenty fire blankets, EXW Telford to DAP Mussafah."""
    request = request_with(
        supplier_currency="GBP",
        fx_rate=FX,
        customs_duty_percent=Decimal("5.0"),
        target_markup_percent=Decimal("45"),
        submission_unit_price=Decimal("3140.00"),
        discloses_principal_price=True,
    )
    request.items = [
        QuoteRequestItem(
            position=0,
            name="CLOTH — Bridela welding blanket",
            unit="Pieces",
            quantity=Decimal("20"),
            cost_rate=Decimal("302.28") * FX,
            rate=Decimal("3140"),
        )
    ]

    def origin(position: int, label: str, gbp: str, firm: bool = False) -> QuoteCostLine:
        return QuoteCostLine(
            position=position,
            stage=CostStage.ORIGIN,
            label=label,
            amount_source=Decimal(gbp),
            source_currency="GBP",
            amount_base=Decimal(gbp) * FX,
            is_firm=firm,
        )

    def destination(position: int, label: str, aed: str) -> QuoteCostLine:
        return QuoteCostLine(
            position=position,
            stage=CostStage.DESTINATION,
            label=label,
            amount_base=Decimal(aed),
        )

    request.cost_lines = [
        origin(1, "UK Certificate of Origin", "110.00", firm=True),
        origin(2, "ABCC legalisation allowance", "250.00"),
        origin(3, "UK inland haulage, tail-lift", "320.00"),
        origin(4, "Origin export handling and AWB", "75.00"),
        origin(5, "Air freight to Abu Dhabi, 167 kg", "850.00"),
        origin(6, "Air cargo insurance", "40.00"),
        destination(7, "UAE clearance, DO and handling", "950.00"),
        destination(8, "Inland delivery to Mussafah", "450.00"),
        destination(9, "Bank remittance charges", "200.00"),
    ]
    return request


# ── the build-up ───────────────────────────────────────────────────────


def test_landed_cost_matches_the_workbook_to_the_cent(cloth):
    """The figures a person worked out by hand for a real bid."""
    landed = bidpack.landed_cost(cloth)

    assert landed.cif_subtotal == Decimal("38068.47")
    assert landed.customs_duty == Decimal("1903.42")
    assert landed.total == Decimal("41571.89")
    assert landed.per_unit == Decimal("2078.5947")
    assert landed.principal_value == Decimal("29925.72")


def test_duty_is_charged_on_the_cif_value_not_the_goods(cloth):
    """Customs charges duty on what arrived, freight and insurance included.

    Charging it on the goods alone understates the bid by the duty on the
    freight — which on an air shipment is most of the difference.
    """
    landed = bidpack.landed_cost(cloth)
    goods_only = bidpack.goods_cost(cloth) * Decimal("0.05")

    assert landed.customs_duty > goods_only
    assert landed.customs_duty == (landed.cif_subtotal * Decimal("0.05")).quantize(
        Decimal("0.01")
    )


def test_the_goods_follow_the_priced_lines(cloth):
    """Repricing from another supplier moves the landed cost with it.

    This is why the goods are derived rather than held as a cost row: a stored
    goods row would still be the old supplier's price after a repricing, sitting
    at the top of the build-up looking exactly as authoritative as the rest.
    """
    before = bidpack.landed_cost(cloth).total
    cloth.items[0].cost_rate = Decimal("250.00") * FX

    after = bidpack.landed_cost(cloth).total
    assert after < before
    assert bidpack.landed_cost(cloth).elements[0].amount_base == Decimal("24750.00")


def test_financing_is_nothing_when_the_money_is_out_for_no_days(cloth):
    cloth.financing_rate_percent = Decimal("7.0")
    cloth.cash_exposure_days = 0

    assert bidpack.landed_cost(cloth).financing_cost == Decimal(0)


def test_financing_is_priced_over_the_days_the_money_is_gone(cloth):
    """A supplier wanting paying before manufacture costs real money."""
    cloth.financing_rate_percent = Decimal("7.0")
    cloth.cash_exposure_days = 150

    landed = bidpack.landed_cost(cloth)
    expected = (
        landed.cif_subtotal * Decimal("0.07") * Decimal(150) / Decimal(365)
    ).quantize(Decimal("0.01"))
    assert landed.financing_cost == expected
    assert landed.total > Decimal("41571.89")


def test_a_derived_row_says_that_it_is_derived(cloth):
    """Somebody checking a cost needs to know what they can argue with."""
    elements = bidpack.landed_cost(cloth).elements
    derived = {e.label for e in elements if e.computed}

    assert "Import duty" in derived
    assert "Goods, as priced from the supplier" in derived
    # The typed rows carry their row id, so a screen can offer to edit them.
    assert all(e.id for e in elements if not e.computed)
    assert all(e.id is None for e in elements if e.computed)


def test_firm_share_separates_what_is_committed_from_what_is_guessed(cloth):
    """Every point of an estimate that comes in high comes out of the margin."""
    cloth.selected_supplier_quote_id = None
    guessy = bidpack.landed_cost(cloth).firm_percent

    # With the goods actually priced from a chosen supplier, most of the bid is
    # committed rather than estimated.
    cloth.selected_supplier_quote_id = "not-none"
    assert bidpack.landed_cost(cloth).firm_percent > guessy


# ── per-unit, and refusing to invent one ───────────────────────────────


def test_no_cost_per_unit_across_lines_in_different_units(cloth):
    """A bid for cable and pumps has no cost "per unit"."""
    cloth.items.append(
        QuoteRequestItem(
            position=1, name="Pump", unit="No", quantity=Decimal(2),
            cost_rate=Decimal(500), rate=Decimal(700),
        )
    )
    cloth.items[0].unit = "m"

    landed = bidpack.landed_cost(cloth)
    assert landed.per_unit is None
    assert landed.quantity is None
    assert "different units" in landed.per_unit_note


def test_several_lines_in_the_same_unit_do_have_a_per_unit(cloth):
    cloth.items.append(
        QuoteRequestItem(
            position=1, name="Second blanket size", unit="Pieces",
            quantity=Decimal("5"), cost_rate=Decimal("100"), rate=Decimal("200"),
        )
    )

    landed = bidpack.landed_cost(cloth)
    assert landed.quantity == Decimal("25")
    assert landed.per_unit is not None


def test_no_priced_lines_says_so_rather_than_dividing_by_nothing():
    landed = bidpack.landed_cost(request_with())

    assert landed.per_unit is None
    assert landed.total == Decimal(0)
    assert "nothing to divide by" in landed.per_unit_note


# ── the ladder ─────────────────────────────────────────────────────────


def test_the_ladder_carries_the_bids_own_markup_in_its_place(cloth):
    cloth.target_markup_percent = Decimal("27")
    ladder = bidpack.scenarios(bidpack.landed_cost(cloth), Decimal("27"))
    rungs = [s.markup_percent for s in ladder]

    assert Decimal("27.00") in rungs
    assert rungs == sorted(rungs)
    assert sum(1 for s in ladder if s.is_target) == 1


def test_markup_and_margin_are_not_the_same_number(cloth):
    """A 45% markup is a 31% margin, and quoting one as the other underprices."""
    target = next(
        s for s in bidpack.scenarios(bidpack.landed_cost(cloth), Decimal("45")) if s.is_target
    )

    assert target.markup_percent == Decimal("45.00")
    assert target.margin_percent == Decimal("31.03")
    assert target.unit_sell == Decimal("3013.96")


def test_a_markup_of_nothing_prices_the_bid_at_cost(cloth):
    landed = bidpack.landed_cost(cloth)
    at_cost = bidpack._scenario(landed, Decimal(0), target=True)

    assert at_cost.total_sell == landed.total
    assert at_cost.margin_percent == Decimal(0)


# ── what actually goes in ──────────────────────────────────────────────


def test_a_rounded_price_beats_the_arithmetic_one(cloth):
    """Rounding a bid up to a clean figure is a decision, not an accident."""
    pack = bidpack.build(cloth)

    assert pack.bid_unit_price == Decimal("3140.00")
    assert pack.bid_total == Decimal("62800.00")
    assert pack.bid_total_is_suggested is False


def test_with_nothing_decided_the_ladder_answers_and_says_it_is_a_suggestion(cloth):
    cloth.submission_unit_price = None
    cloth.submission_total = None

    pack = bidpack.build(cloth)
    assert pack.bid_total_is_suggested is True
    assert pack.bid_total == pack.target.total_sell


def test_an_explicit_total_wins_over_a_unit_price(cloth):
    """On a bid discounted as a package, the total is the decision."""
    cloth.submission_total = Decimal("60000.00")

    pack = bidpack.build(cloth)
    assert pack.bid_total == Decimal("60000.00")
    assert pack.bid_total_is_suggested is False


# ── what the buyer sees ────────────────────────────────────────────────


def test_the_uplift_the_buyer_reads_and_the_margin_we_keep_are_different(cloth):
    """The gap between them is the whole reason a price breakdown is attached.

    Our price is 110% above what we paid the OEM. We keep 34% of it. The rest
    is freight, duty, documentation and clearance — and unless that is said, in
    a breakdown, alongside the principal's quotation, the buyer reads the first
    number and draws the obvious conclusion.
    """
    d = bidpack.build(cloth).disclosure

    assert d.principal_value == Decimal("29925.72")
    assert d.apparent_uplift_percent == Decimal("109.85")
    assert d.true_margin_percent == Decimal("33.80")
    assert d.recoverable_cost == Decimal("11646.17")
    assert d.disclosed is True


def test_no_principal_value_means_no_uplift_rather_than_a_division_by_zero():
    """A quote with nothing priced yet still has to render."""
    d = bidpack.build(request_with()).disclosure

    assert d.apparent_uplift_percent is None
    assert d.true_margin_percent is None


# ── the red flags ──────────────────────────────────────────────────────


def flag(ref: str, severity: Severity | None, *, position: int, resolved=None, status=None):
    return QuoteComplianceItem(
        position=position,
        ref=ref,
        requirement=f"requirement {ref}",
        status=status or ComplianceStatus.DEVIATION,
        severity=severity,
        resolved_at=resolved,
    )


def test_red_flags_come_out_worst_first(cloth):
    cloth.compliance = [
        flag("C1", Severity.MEDIUM, position=0),
        flag("C2", Severity.STOPPER, position=1),
        flag("C3", None, position=2),
        flag("C4", Severity.HIGH, position=3),
    ]

    flags = bidpack.red_flags(cloth)
    assert [f.ref for f in flags] == ["C2", "C4", "C1"]


def test_a_cleared_flag_sinks_below_the_live_ones_rather_than_vanishing(cloth):
    """What was cured, and when, is what a post-mortem on a lost bid asks."""
    from datetime import UTC, datetime

    cloth.compliance = [
        flag("C1", Severity.STOPPER, position=0, resolved=datetime.now(UTC)),
        flag("C2", Severity.MEDIUM, position=1),
    ]

    flags = bidpack.red_flags(cloth)
    assert [f.ref for f in flags] == ["C2", "C1"]
    assert flags[-1].resolved is True


# ── the warnings ───────────────────────────────────────────────────────


def test_an_unresolved_stopper_is_said_outright(cloth):
    cloth.compliance = [flag("E32", Severity.STOPPER, position=0)]

    assert any("stopping the submission" in w for w in bidpack.warnings(cloth))


def test_a_mandatory_portal_field_with_nothing_in_it_is_named(cloth):
    cloth.submission_fields = [
        QuoteSubmissionField(position=0, label="Country of origin", is_mandatory=True),
        QuoteSubmissionField(
            position=1, label="Unit price", is_mandatory=True, value="3140.00"
        ),
    ]

    warnings = bidpack.warnings(cloth)
    assert any("Country of origin" in w for w in warnings)
    assert not any("Unit price" in w for w in warnings)


def test_an_optional_field_left_empty_is_not_a_warning(cloth):
    cloth.submission_fields = [
        QuoteSubmissionField(position=0, label="Comments", is_mandatory=False)
    ]

    assert bidpack.warnings(cloth) == []


def test_warnings_are_advisory_and_a_quote_with_none_is_quiet(cloth):
    """Nothing here takes a button away. See ``bidpack.warnings``."""
    cloth.discloses_principal_price = False
    cloth.bid_validity_days = None

    assert bidpack.warnings(cloth) == []

# ── carrying the supplier's own terms across ───────────────────────────
#
# Choosing a supplier takes their prices. It should also take everything else
# they stated, because a quotation's validity, payment term, warranty position
# and Incoterm are each an answer to a clause of the RFP — and retyping them
# into a matrix by hand is how they come to be answered from memory a fortnight
# later. These check that it happens, and that it stops short of overwriting
# what a person has already decided.


def supplier(**over) -> SupplierQuote:
    items = over.pop("items", [])
    quote = SupplierQuote(
        supplier_name=over.pop("supplier_name", "IC International"),
        currency=over.pop("currency", "GBP"),
        fx_rate=over.pop("fx_rate", FX),
        **over,
    )
    quote.items = items
    return quote


def test_the_money_comes_across_so_the_build_up_has_a_rate():
    request = request_with()
    service.absorb_supplier(request, supplier())

    assert request.supplier_currency == "GBP"
    assert request.fx_rate == FX


def test_a_rate_somebody_already_set_is_not_overwritten():
    """Their judgement about the spread is not ours to replace."""
    request = request_with(supplier_currency="GBP", fx_rate=Decimal("5.05"))
    service.absorb_supplier(request, supplier(fx_rate=Decimal("4.80")))

    assert request.fx_rate == Decimal("5.05")


def test_the_manufacturer_comes_across_when_the_lines_agree_on_one():
    request = request_with()
    service.absorb_supplier(
        request,
        supplier(
            items=[
                SupplierQuoteItem(
                    position=0, description="a", brand="Bridela", part_number="DHO/011"
                )
            ]
        ),
    )

    assert request.manufacturer_name == "Bridela"
    assert request.manufacturer_part_number == "DHO/011"


def test_nothing_is_guessed_when_the_lines_disagree():
    """Two brands on one quotation is not a manufacturer, it is a question."""
    request = request_with()
    service.absorb_supplier(
        request,
        supplier(
            items=[
                SupplierQuoteItem(position=0, description="a", brand="Bridela"),
                SupplierQuoteItem(position=1, description="b", brand="Something else"),
            ]
        ),
    )

    assert request.manufacturer_name is None


def test_a_prepayment_term_arrives_already_marked_as_a_deviation():
    request = request_with()
    service.absorb_supplier(
        request, supplier(payment_terms="100% pre-payment against proforma invoice")
    )

    row = next(r for r in request.compliance if r.ref == "S-PAY")
    assert row.status == ComplianceStatus.DEVIATION
    assert row.severity == Severity.HIGH
    assert "pre-payment" in row.supplier_position


def test_an_ordinary_payment_term_arrives_as_a_question_not_a_verdict():
    """Where the answer needs a person to read a sentence, it stays open."""
    request = request_with()
    service.absorb_supplier(request, supplier(payment_terms="30 days net from invoice"))

    row = next(r for r in request.compliance if r.ref == "S-PAY")
    assert row.status == ComplianceStatus.OPEN
    assert row.severity is None


def test_no_warranty_is_read_as_non_compliant():
    request = request_with()
    service.absorb_supplier(request, supplier(warranty="No warranty is applicable."))

    row = next(r for r in request.compliance if r.ref == "S-WAR")
    assert row.status == ComplianceStatus.NON_COMPLIANT
    assert row.severity == Severity.HIGH


def test_an_incoterm_that_matches_the_rfp_is_compliant():
    """Compared on the term alone — the place is a field of its own."""
    request = request_with(incoterm_required="DAP", incoterm_place="Mussafah")
    service.absorb_supplier(request, supplier(incoterms="DAP Mussafah"))

    row = next(r for r in request.compliance if r.ref == "S-INC")
    assert row.status == ComplianceStatus.COMPLIANT
    assert row.severity is None


def test_an_incoterm_that_does_not_match_is_where_the_freight_cost_comes_from():
    request = request_with(incoterm_required="DAP", incoterm_place="Mussafah")
    service.absorb_supplier(request, supplier(incoterms="EXW Telford"))

    row = next(r for r in request.compliance if r.ref == "S-INC")
    assert row.status == ComplianceStatus.DEVIATION
    assert row.severity == Severity.HIGH


def test_an_unstated_incoterm_raises_nothing_rather_than_a_false_mismatch():
    request = request_with(incoterm_required="DAP")
    service.absorb_supplier(request, supplier(incoterms=None))

    assert not any(r.ref == "S-INC" for r in request.compliance)


def test_repricing_from_another_supplier_updates_their_words_not_our_decisions():
    """A person's owner and action survive the supplier changing under them."""
    request = request_with()
    service.absorb_supplier(request, supplier(warranty="No warranty applicable."))
    row = next(r for r in request.compliance if r.ref == "S-WAR")
    row.owner = "Fasna"
    row.action = "Negotiate 12 months."
    before = len(request.compliance)

    service.absorb_supplier(
        request, supplier(supplier_name="Another firm", warranty="12 months from delivery.")
    )

    assert len(request.compliance) == before
    assert row.owner == "Fasna"
    assert row.action == "Negotiate 12 months."
    assert "Another firm" in row.supplier_position
    assert row.status == ComplianceStatus.OPEN


def test_a_cleared_row_is_not_reopened_by_a_new_supplier():
    from datetime import UTC, datetime

    request = request_with()
    service.absorb_supplier(request, supplier(warranty="No warranty applicable."))
    row = next(r for r in request.compliance if r.ref == "S-WAR")
    row.resolved_at = datetime.now(UTC)

    service.absorb_supplier(request, supplier(warranty="Still no warranty."))
    assert row.status == ComplianceStatus.NON_COMPLIANT
    assert row.resolved_at is not None


def test_the_portal_checklist_is_seeded_once_and_never_on_top_of_anybody():
    request = request_with()
    service.seed_submission_checklist(request)
    assert len(request.submission_fields) == 6

    request.submission_fields = [QuoteSubmissionField(position=0, label="Mine")]
    service.seed_submission_checklist(request)
    assert [f.label for f in request.submission_fields] == ["Mine"]


# ── the conversion ─────────────────────────────────────────────────────


def test_a_cost_quoted_in_the_suppliers_currency_converts_itself():
    """Storing one number twice is how a build-up and its rate come apart."""
    request = request_with(supplier_currency="GBP", fx_rate=FX)
    service.set_cost_lines(
        request,
        [
            {
                "label": "Air freight",
                "amount_source": "850.00",
                "source_currency": "GBP",
                # Deliberately wrong. The rate decides, not the caller.
                "amount_base": "1.00",
            }
        ],
    )

    assert request.cost_lines[0].amount_base == Decimal("850.00") * FX


def test_a_cost_in_our_own_money_is_taken_as_given():
    request = request_with(supplier_currency="GBP", fx_rate=FX)
    service.set_cost_lines(request, [{"label": "Inland delivery", "amount_base": "450.00"}])

    assert request.cost_lines[0].amount_base == Decimal("450.00")


def test_with_no_rate_a_foreign_figure_is_not_invented():
    request = request_with()
    service.set_cost_lines(
        request,
        [
            {
                "label": "Freight",
                "amount_source": "850",
                "source_currency": "GBP",
                "amount_base": "0",
            }
        ],
    )

    assert request.cost_lines[0].amount_base == Decimal(0)
    assert request.cost_lines[0].amount_source == Decimal("850")

# ── the response that has to render ────────────────────────────────────


def test_the_derived_block_serialises_before_anything_has_been_flushed():
    """A row added in this request is read back in this request's response.

    ``is_firm`` and ``is_principal`` carry column defaults, and a column default
    is applied by the database at insert — so a row that has been added to the
    session but not yet flushed reads ``None``, not ``False``. The response for
    the very request that added it is built in exactly that window, and a
    boolean field handed ``None`` is a 500 rather than a quote.
    """
    request = request_with(supplier_currency="GBP", fx_rate=FX)
    request.items = [
        QuoteRequestItem(
            position=0, name="x", unit="Pieces", quantity=Decimal(20),
            cost_rate=Decimal(100), rate=Decimal(200),
        )
    ]
    # Constructed the way SQLAlchemy hands one back before a flush: the flags
    # are simply absent.
    request.cost_lines = [QuoteCostLine(position=0, label="Freight", amount_base=Decimal(500))]

    body = BidPackOut.model_validate(bidpack.build(request), from_attributes=True)
    freight = next(e for e in body.landed.elements if e.label == "Freight")

    assert freight.is_firm is False
    assert freight.is_principal is False
    assert body.model_dump(mode="json")["landed"]["total"] == "2500.00"


def test_a_status_read_back_as_a_plain_string_still_serialises():
    """Postgres hands these back as text, not as enum members.

    The columns are ``String`` — deliberately, so a new status is not a
    migration — which means nothing guarantees an enum member arrives here.
    """
    request = request_with()
    request.compliance = [
        QuoteComplianceItem(
            position=0, ref="C3", requirement="Bid validity",
            status="non_compliant", severity="critical",
        )
    ]

    body = BidPackOut.model_validate(bidpack.build(request), from_attributes=True)
    assert body.red_flags[0].severity == Severity.CRITICAL
    assert body.model_dump(mode="json")["red_flags"][0]["severity"] == "critical"

