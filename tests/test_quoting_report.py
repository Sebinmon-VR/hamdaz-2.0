"""The selling & costing report: the figures an approver decides on.

No database. The whole report is a function of a request object and its rows,
so it is attacked directly.

The fixture is a real one: quote QT-001720, three HPE drives bought from a web
shop in dollars and sold to ADNOC, costed on the report presales prepared by
hand on 24 September 2026. Every headline figure asserted here is that
report's, to the cent — the quoted price, the landed cost, the margin, the
walk-away, the per-line split, the landed-cost build-up and the quote value.
Where that report's own arithmetic wobbled by a cent between rows (it was
worked out in floating point), the assertions pin this module's rule instead
and say so.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from app.models.quoting import CostStage, QuoteCostLine, QuoteRequest, QuoteRequestItem
from app.quoting import report as report_mod
from app.quoting import report_pdf
from app.quoting.report import ACCEPTABLE, COMFORTABLE, LOSS, NEEDS_APPROVAL

#: 1 USD in AED, as the report states it.
RATE = Decimal("3.6725")


def request_with(**overrides) -> QuoteRequest:
    fields = {
        "title": "HPE drives for ADNOC",
        "customer_name": "ADNOC",
        "currency": "USD",
        "customs_duty_percent": Decimal(0),
        "financing_rate_percent": Decimal(0),
        "cash_exposure_days": 0,
        "discloses_principal_price": False,
        "multiple_supplier_quotes": False,
        # Column defaults are applied by the database, and nothing here has
        # been near one.
        "discount": Decimal(0),
        "shipping_charge": Decimal(0),
        "adjustment": Decimal(0),
        **overrides,
    }
    request = QuoteRequest(**fields)
    request.items = []
    request.cost_lines = []
    request.compliance = []
    request.submission_fields = []
    request.created_at = datetime(2026, 9, 24, 9, 0)
    return request


def line(position: int, code: str, name: str, cost: str, sell: str) -> QuoteRequestItem:
    return QuoteRequestItem(
        position=position,
        item_code=code,
        name=name,
        quantity=Decimal(1),
        cost_rate=Decimal(cost),
        rate=Decimal(sell),
        discount=Decimal(0),
    )


@pytest.fixture
def drives() -> QuoteRequest:
    """QT-001720: three drives from router-switch.com, express courier to Abu Dhabi."""
    request = request_with(
        reference="QT-001720",
        reference_number="6000148693",
        tax_name="VAT",
        tax_percentage=Decimal(5),
        place_of_supply="Abu Dhabi",
        quote_date=datetime(2026, 9, 24),
        expiry_date=datetime(2026, 10, 24),
        customs_duty_percent=Decimal("5"),
        supplier_name="router-switch.com",
        supplier_basis="online purchase",
        supplier_route="Express courier to Abu Dhabi",
    )
    request.items = [
        line(0, "881457-B21", "HPE 2.4TB SAS 12G 10K SFF HDD", "637", "2125.70"),
        line(1, "872475-B21", "HPE 300GB SAS 12G 10K SFF HDD", "181", "536.50"),
        line(2, "872479-B21", "HPE 1.2TB SAS 12G 10K SFF HDD", "462", "1326.74"),
    ]
    request.cost_lines = [
        QuoteCostLine(
            position=1, stage=CostStage.ORIGIN, label="Freight – express courier",
            amount_base=Decimal("60"),
        ),
        QuoteCostLine(
            position=2, stage=CostStage.ORIGIN, label="Insurance",
            percent=Decimal("1"), percent_of="goods", amount_base=Decimal(0),
        ),
        QuoteCostLine(
            position=3, stage=CostStage.DESTINATION, label="Clearance fees",
            amount_base=Decimal("30"),
        ),
        QuoteCostLine(
            position=4, stage=CostStage.DESTINATION, label="Payment / bank charges",
            percent=Decimal("3"), percent_of="goods", amount_base=Decimal(0),
        ),
        QuoteCostLine(
            position=5, stage=CostStage.DESTINATION, label="Local delivery",
            amount_base=Decimal("15"),
        ),
    ]
    return request


@pytest.fixture
def report(drives):
    return report_mod.build(
        drives, base_rate=RATE, rate_source="test", prepared_on=date(2026, 9, 24)
    )


# ── the four tiles ─────────────────────────────────────────────────────


def test_the_headline_figures_match_the_report_presales_prepared(report) -> None:
    assert (report.quoted_price.amount, report.quoted_price.base) == (
        Decimal("3988.94"), Decimal("14649.38"),
    )
    assert (report.landed_total.amount, report.landed_total.base) == (
        Decimal("1503.84"), Decimal("5522.85"),
    )
    assert (report.gross_margin.amount, report.gross_margin.base) == (
        Decimal("2485.10"), Decimal("9126.53"),
    )
    assert report.gross_margin_percent == Decimal("62.30")
    assert (report.walk_away_price.amount, report.walk_away_price.base) == (
        Decimal("2005.12"), Decimal("7363.80"),
    )


def test_margin_is_a_share_of_the_selling_price_not_a_markup_on_cost(report) -> None:
    """62.3% of what the customer pays. On cost the same quote is a 165%
    markup, and quoting one as the other misprices the bid."""
    assert report.gross_margin_percent == (
        report.gross_margin.amount / report.quoted_price.amount * 100
    ).quantize(Decimal("0.01"))


# ── section 1: by line ─────────────────────────────────────────────────


def test_the_landed_cost_is_shared_out_by_what_each_line_cost(report) -> None:
    """Freight is paid on the shipment, so the line that is half the goods
    carries half the freight. The figures are the hand-prepared report's."""
    first, second, third = report.lines
    assert first.part_number == "881457-B21"
    assert (first.landed.amount, first.landed.base) == (Decimal("748.40"), Decimal("2748.48"))
    assert (first.margin.amount, first.margin.base) == (Decimal("1377.30"), Decimal("5058.15"))
    assert first.margin_percent == Decimal("64.79")
    assert (second.landed.amount, second.landed.base) == (Decimal("212.65"), Decimal("780.97"))
    assert (second.margin.amount, second.margin.base) == (Decimal("323.85"), Decimal("1189.33"))
    assert (third.landed.amount, third.landed.base) == (Decimal("542.79"), Decimal("1993.40"))
    assert (third.margin.amount, third.margin.base) == (Decimal("783.95"), Decimal("2879.05"))


def test_the_line_split_adds_up_to_the_total_it_was_split_from(report) -> None:
    assert sum(row.landed.amount for row in report.lines) == report.landed_total.amount
    assert sum(row.landed.base for row in report.lines) == report.landed_total.base
    assert sum(row.margin.amount for row in report.lines) == report.gross_margin.amount


def test_the_supplier_column_is_what_the_supplier_charged(report) -> None:
    assert [row.supplier_amount for row in report.lines] == [
        Decimal("637.00"), Decimal("181.00"), Decimal("462.00"),
    ]
    assert report.total_supplier_amount == Decimal("1280.00")
    assert report.supplier_currency == "USD"
    assert report.total_quantity == Decimal(3)


def test_a_line_with_no_cost_behind_it_carries_no_landed_cost_and_says_so(drives) -> None:
    drives.items[1].cost_rate = None
    built = report_mod.build(drives, base_rate=RATE)
    assert built.lines[1].landed is None
    assert built.lines[1].margin is None
    assert built.lines[1].margin_percent is None
    assert any("no supplier cost" in w for w in built.warnings)
    # The other two still share the whole landed cost between them.
    assert sum(row.landed.amount for row in built.lines if row.landed) == built.landed_total.amount


# ── section 2: the landed cost ─────────────────────────────────────────


def test_the_build_up_is_the_hand_prepared_one_row_for_row(report) -> None:
    rows = {r.label: r for r in report.cost_rows}
    assert rows["Goods, as priced from the supplier"].amount.base == Decimal("4700.80")
    assert rows["Freight – express courier"].amount.amount == Decimal("60.00")
    assert rows["Freight – express courier"].amount.base == Decimal("220.35")
    assert rows["Insurance (1%)"].amount.amount == Decimal("12.80")
    assert rows["Insurance (1%)"].amount.base == Decimal("47.01")
    assert rows["Import duty"].amount.amount == Decimal("67.64")
    assert rows["Import duty"].amount.base == Decimal("248.41")
    assert rows["Clearance fees"].amount.base == Decimal("110.18")
    assert rows["Payment / bank charges (3%)"].amount.amount == Decimal("38.40")
    assert rows["Payment / bank charges (3%)"].amount.base == Decimal("141.02")
    assert rows["Local delivery"].amount.base == Decimal("55.09")


def test_a_rated_row_follows_the_goods_when_the_supplier_price_moves(drives) -> None:
    """Insurance at 1% is 1% of whatever the goods now cost, not a stale figure."""
    before = {r.label: r.amount.amount for r in report_mod.build(drives).cost_rows}
    drives.items[0].cost_rate = Decimal("1637")
    after = {r.label: r.amount.amount for r in report_mod.build(drives).cost_rows}
    assert before["Insurance (1%)"] == Decimal("12.80")
    assert after["Insurance (1%)"] == Decimal("22.80")
    assert after["Payment / bank charges (3%)"] == Decimal("68.40")


def test_duty_is_charged_on_the_cif_value_including_the_rated_rows(drives) -> None:
    """5% of 1,280 + 60 + 12.80 — the insurance counts towards the duty base."""
    built = report_mod.build(drives)
    duty = next(r for r in built.cost_rows if r.label == "Import duty")
    assert duty.amount.amount == (Decimal("1352.80") * Decimal("0.05")).quantize(Decimal("0.01"))


def test_estimates_are_starred_and_the_footnote_names_them(report) -> None:
    starred = [r.label for r in report.cost_rows if r.is_estimate]
    assert starred == ["Freight – express courier", "Clearance fees", "Local delivery"]
    assert any(
        note.startswith("* Freight") and "router-switch.com" in note for note in report.notes
    )
    assert any("Import VAT is recoverable" in note for note in report.notes)


# ── section 3: the quote value and the walk-away ───────────────────────


def test_the_quote_value_carries_the_tax_on_the_total(report) -> None:
    """The quote's own tax: 5% of the total before tax, once — 199.45, the
    figure on the hand-prepared report. Rounded line by line it came to a
    cent more."""
    assert report.tax_label == "VAT 5%"
    assert (report.tax_total.amount, report.tax_total.base) == (
        Decimal("199.45"), Decimal("732.48"),
    )
    assert (report.total_incl_tax.amount, report.total_incl_tax.base) == (
        Decimal("4188.39"), Decimal("15381.86"),
    )


def test_the_walk_away_ladder_sits_five_points_either_side_of_the_floor(report) -> None:
    rungs = {r.margin_percent: r for r in report.walk_away_ladder}
    assert set(rungs) == {Decimal("30.00"), Decimal("25.00"), Decimal("20.00")}
    assert rungs[Decimal("30.00")].price.amount == Decimal("2148.34")
    assert rungs[Decimal("30.00")].price.base == Decimal("7889.79")
    assert rungs[Decimal("30.00")].max_discount_percent == Decimal("46.14")
    assert rungs[Decimal("25.00")].price.amount == Decimal("2005.12")
    assert rungs[Decimal("25.00")].max_discount_percent == Decimal("49.73")
    assert rungs[Decimal("20.00")].price.amount == Decimal("1879.80")
    assert rungs[Decimal("20.00")].price.base == Decimal("6903.57")
    assert rungs[Decimal("20.00")].max_discount_percent == Decimal("52.87")


def test_the_walk_away_is_the_price_at_which_the_margin_is_the_floor(report) -> None:
    floor = report.walk_away_price.amount
    margin = ((floor - report.landed_total.amount) / floor * 100).quantize(Decimal("0.01"))
    assert margin == Decimal("25.00")


# ── section 4: the negotiation ─────────────────────────────────────────


def test_each_discount_step_says_what_it_leaves_and_whether_that_is_fine(report) -> None:
    steps = {s.discount_percent: s for s in report.negotiation}
    assert [s.discount_percent for s in report.negotiation] == [
        Decimal(0), Decimal(10), Decimal(20), Decimal(30), Decimal(40), Decimal(50),
    ]
    quoted = steps[Decimal(0)]
    assert quoted.total_incl_tax.amount == Decimal("4188.39")
    assert quoted.margin.amount == Decimal("2485.10")
    assert quoted.status == COMFORTABLE

    ten = steps[Decimal(10)]
    assert ten.total_incl_tax.amount == Decimal("3769.55")
    assert ten.margin.amount == Decimal("2086.21")
    assert ten.margin_percent == Decimal("58.11")
    assert ten.status == COMFORTABLE

    assert steps[Decimal(30)].margin.amount == Decimal("1288.42")
    assert steps[Decimal(30)].margin_percent == Decimal("46.14")
    assert steps[Decimal(30)].status == COMFORTABLE

    forty = steps[Decimal(40)]
    assert forty.total_incl_tax.amount == Decimal("2513.03")
    assert forty.margin.amount == Decimal("889.52")
    assert forty.margin_percent == Decimal("37.17")
    assert forty.status == ACCEPTABLE

    fifty = steps[Decimal(50)]
    assert fifty.margin.amount == Decimal("490.63")
    assert fifty.margin_percent == Decimal("24.60")
    assert fifty.status == NEEDS_APPROVAL


def test_the_base_currency_is_converted_once_from_the_unrounded_figure(report) -> None:
    """The hand-prepared report's own AED column wobbled by a cent from row to
    row. The rule here is one rule: convert the unrounded figure, round once."""
    thirty = next(r for r in report.walk_away_ladder if r.margin_percent == 30)
    unrounded = Decimal("1503.84") / Decimal("0.7")
    assert thirty.price.base == (unrounded * RATE).quantize(Decimal("0.01"))
    assert thirty.price.base != (thirty.price.amount * RATE).quantize(Decimal("0.01"))


def test_the_recommendation_reads_off_the_ladder(report) -> None:
    assert report.recommendation == (
        "Counter at 10%, then 20%; 30% as the final offer. Anything past that needs "
        "management approval. Do not go below USD 2,005.12 / AED 7,363.80 ex-VAT."
    )


def test_a_typed_recommendation_beats_the_generated_one(drives) -> None:
    drives.recommendation = "Hold at list — the customer has no alternative source."
    assert report_mod.build(drives).recommendation == drives.recommendation


def test_the_house_lines_can_be_moved_per_quote(drives) -> None:
    drives.walk_away_margin_percent = Decimal("35")
    drives.comfortable_margin_percent = Decimal("55")
    built = report_mod.build(drives)
    steps = {s.discount_percent: s for s in built.negotiation}
    assert steps[Decimal(10)].status == COMFORTABLE  # 58.1%
    assert steps[Decimal(20)].status == ACCEPTABLE  # 52.9%
    assert steps[Decimal(40)].status == ACCEPTABLE  # 37.2%
    assert steps[Decimal(50)].status == NEEDS_APPROVAL  # 24.6%
    assert built.walk_away_price.amount == (Decimal("1503.84") / Decimal("0.65")).quantize(
        Decimal("0.01")
    )
    assert "any discount" not in built.recommendation


def test_a_thin_quote_is_told_to_hold_its_price(drives) -> None:
    for item in drives.items:
        item.rate = item.cost_rate * Decimal("1.4")
    built = report_mod.build(drives)
    assert built.gross_margin_percent < Decimal(40)
    assert built.recommendation.startswith("Hold the quoted price")


# ── the parties and the header ─────────────────────────────────────────


def test_the_customer_and_supplier_blocks_say_what_the_page_says(report) -> None:
    assert report.reference == "QT-001720"
    assert report.customer.name == "ADNOC"
    assert report.customer.reference == "6000148693"
    assert report.customer.place_of_supply == "Abu Dhabi"
    assert (report.customer.valid_from, report.customer.valid_until) == (
        date(2026, 9, 24), date(2026, 10, 24),
    )
    assert report.supplier.name == "router-switch.com"
    assert report.supplier.basis == "online purchase"
    assert report.supplier.route == "Express courier to Abu Dhabi"
    assert report.currency == "USD"
    assert report.base_currency == "AED"
    assert report.base_rate == RATE


def test_a_quote_in_the_base_currency_is_a_single_currency_report(drives) -> None:
    drives.currency = "AED"
    built = report_mod.build(drives, base_rate=RATE)
    assert built.base_rate is None
    assert built.quoted_price.base is None
    assert built.lines[0].landed.base is None


def test_without_a_rate_the_report_says_so_rather_than_guessing(drives) -> None:
    built = report_mod.build(drives)
    assert built.base_rate is None
    assert any("USD only" in w for w in built.warnings)


def test_footnotes_typed_on_the_quote_follow_the_generated_ones(drives) -> None:
    drives.report_notes = (
        "Confirm HPE warranty and COO (China) before PO.\n\nAllow 5–10 working days."
    )
    built = report_mod.build(drives)
    assert built.notes[-2:] == [
        "Confirm HPE warranty and COO (China) before PO.",
        "Allow 5–10 working days.",
    ]


# ── the PDF ────────────────────────────────────────────────────────────


def test_the_pdf_renders_and_carries_the_figures(drives) -> None:
    pdf = report_pdf.build(drives, base_rate=RATE, prepared_on=date(2026, 9, 24))
    assert pdf.startswith(b"%PDF")

    import io

    import pdfplumber

    with pdfplumber.open(io.BytesIO(pdf)) as document:
        assert len(document.pages) == 1
        text = document.pages[0].extract_text()
    for expected in (
        "SELLING & COSTING REPORT", "QT-001720", "1 USD = 3.6725 AED", "ADNOC",
        "router-switch.com", "3,988.94", "14,649.38", "1,503.84", "5,522.85",
        "2,485.10", "62.3%", "2,005.12", "7,363.80", "881457-B21", "748.40",
        "Insurance (1%)", "Payment / bank charges (3%)", "VAT 5%", "4,188.39",
        "Selling price = cost",
        "Healthy margin", "Below walk-away", "Counter at 10%", "Prepared by",
    ):
        assert expected in text, expected


def test_a_long_quote_flows_onto_a_second_page_under_the_same_letterhead(drives) -> None:
    drives.items = [
        line(i, f"P-{i:03d}", f"Line {i}", "10", "25") for i in range(60)
    ]
    pdf = report_pdf.build(drives, base_rate=RATE)

    import io

    import pdfplumber

    with pdfplumber.open(io.BytesIO(pdf)) as document:
        assert len(document.pages) >= 2
        assert "SELLING & COSTING REPORT" in document.pages[1].extract_text()


def test_the_file_is_named_after_the_quote(drives) -> None:
    assert report_pdf.filename_for(drives) == "QT-001720_Selling_and_Costing_Report.pdf"

def test_a_step_is_healthy_acceptable_below_the_walk_away_or_a_loss() -> None:
    """Four plain words, decided from the two thresholds and nothing else."""
    status = report_mod._status
    walk_away, comfortable = Decimal(25), Decimal(40)
    assert status(Decimal("45"), walk_away, comfortable) == COMFORTABLE
    assert status(Decimal("40"), walk_away, comfortable) == COMFORTABLE
    assert status(Decimal("30"), walk_away, comfortable) == ACCEPTABLE
    assert status(Decimal("25"), walk_away, comfortable) == ACCEPTABLE
    assert status(Decimal("10"), walk_away, comfortable) == NEEDS_APPROVAL
    assert status(Decimal("0"), walk_away, comfortable) == NEEDS_APPROVAL
    assert status(Decimal("-14.3"), walk_away, comfortable) == LOSS
    assert status(None, walk_away, comfortable) == LOSS


def test_the_signature_lines_stay_blank_until_somebody_has_decided(report) -> None:
    assert report.reviewed_by is None
    assert report.approved_by is None
