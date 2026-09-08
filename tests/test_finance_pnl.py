"""The profit and loss arithmetic.

No database and no network: everything here is the pure path from Zoho-shaped
dictionaries to a finished statement, which is the part where a mistake is
invisible. A wrong subtotal does not raise, it simply reports the wrong profit,
and the only thing standing between that and a board pack is these tests.

The fixtures are deliberately small enough to add up in your head. If a test
fails, the expected figure should be checkable on paper rather than by rerunning
the code that produced it.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.finance.accounts import Chart, Section
from app.finance.ledger import (
    Walk,
    from_bills,
    from_credit_notes,
    from_expenses,
    from_invoices,
    from_journals,
    from_vendor_credits,
    money,
)
from app.finance.pnl import build, by_month, drill
from app.finance.service import resolve

PERIOD = (date(2026, 1, 1), date(2026, 1, 31))

#: One account of each kind that matters, plus two that must be ignored.
CHART_ROWS = [
    {"account_id": "1", "account_name": "Sales", "account_type": "income", "account_code": "400"},
    {"account_id": "2", "account_name": "Cost of sales", "account_type": "cost_of_goods_sold"},
    {"account_id": "3", "account_name": "Rent", "account_type": "expense"},
    {"account_id": "4", "account_name": "Interest earned", "account_type": "other_income"},
    {"account_id": "5", "account_name": "Bank charges", "account_type": "other_expense"},
    # Balance sheet accounts: the other half of every entry, and never a P&L line.
    {"account_id": "9", "account_name": "Bank", "account_type": "bank"},
    {"account_id": "10", "account_name": "Debtors", "account_type": "accounts_receivable"},
]


@pytest.fixture
def chart() -> Chart:
    return Chart.from_zoho(CHART_ROWS)


def _invoice(total, lines, *, day=15, status="sent", rate=None):
    row = {
        "invoice_id": f"inv-{day}-{total}",
        "invoice_number": f"INV-{day}",
        "customer_name": "A Customer",
        "date": f"2026-01-{day:02d}",
        "status": status,
        "total": total,
        "line_items": lines,
    }
    if rate is not None:
        row["exchange_rate"] = rate
    return row


# ── the chart ──────────────────────────────────────────────────────────


def test_chart_keeps_only_profit_and_loss_accounts(chart):
    """Balance sheet accounts are read and then deliberately dropped."""
    assert len(chart) == 5
    assert chart.total_accounts == 7
    assert chart.section_of("1") is Section.INCOME
    assert chart.section_of("9") is None, "a bank account is not a P&L line"
    assert chart.section_of("10") is None
    assert chart.section_of(None) is None
    assert chart.section_of("does-not-exist") is None


# ── signs, which is where this goes wrong ──────────────────────────────


def test_invoice_credits_income(chart):
    walk = from_invoices(
        [_invoice(1000, [{"account_id": "1", "item_total": 1000}])], chart, PERIOD
    )
    assert walk.postings[0].credit == money(1000)
    assert walk.postings[0].debit == money(0)
    assert walk.postings[0].amount == money(1000), "income reads positive"


def test_credit_note_reduces_income(chart):
    """A refund must subtract from revenue, not add to it."""
    rows = [
        {
            "creditnote_id": "cn-1",
            "creditnote_number": "CN-1",
            "date": "2026-01-20",
            "status": "open",
            "total": 200,
            "line_items": [{"account_id": "1", "item_total": 200}],
        }
    ]
    walk = from_credit_notes(rows, chart, PERIOD)
    assert walk.postings[0].amount == money(-200)


def test_bill_debits_expense(chart):
    rows = [
        {
            "bill_id": "b-1",
            "bill_number": "B-1",
            "date": "2026-01-10",
            "status": "open",
            "total": 300,
            "line_items": [{"account_id": "3", "item_total": 300}],
        }
    ]
    walk = from_bills(rows, chart, PERIOD)
    assert walk.postings[0].debit == money(300)
    assert walk.postings[0].amount == money(300), "a cost reads positive and is subtracted"


def test_vendor_credit_reduces_cost(chart):
    """The sign that is easiest to get backwards, and worst when it is.

    A supplier refund posted as a debit would not merely be ignored — it would
    *add* to costs, putting the error at twice the value of the credit and in
    the wrong direction.
    """
    rows = [
        {
            "vendor_credit_id": "vc-1",
            "vendor_credit_number": "VC-1",
            "date": "2026-01-12",
            "status": "open",
            "total": 50,
            "line_items": [{"account_id": "3", "item_total": 50}],
        }
    ]
    walk = from_vendor_credits(rows, chart, PERIOD)
    posting = walk.postings[0]
    assert posting.credit == money(50)
    assert posting.amount == money(-50)
    assert posting.source_id == "vc-1", "the id field is vendor_credit_id, not vendorcredit_id"


def test_journal_honours_its_own_sides(chart):
    rows = [
        {
            "journal_id": "j-1",
            "entry_number": "J-1",
            "journal_date": "2026-01-05",
            "status": "published",
            "line_items": [
                {"account_id": "3", "debit_or_credit": "debit", "amount": 75},
                {"account_id": "9", "debit_or_credit": "credit", "amount": 75},
            ],
        }
    ]
    walk = from_journals(rows, chart, PERIOD)
    assert len(walk.postings) == 1, "the bank side is not a P&L posting"
    assert walk.postings[0].amount == money(75)


def test_unpublished_journal_does_not_post(chart):
    rows = [
        {
            "journal_id": "j-2",
            "journal_date": "2026-01-05",
            "status": "draft",
            "line_items": [{"account_id": "3", "debit_or_credit": "debit", "amount": 999}],
        }
    ]
    walk = from_journals(rows, chart, PERIOD)
    assert walk.postings == []
    assert walk.skipped["journals:draft"] == 1


# ── what must not be counted ───────────────────────────────────────────


@pytest.mark.parametrize("status", ["draft", "void"])
def test_draft_and_void_invoices_do_not_post(chart, status):
    """Counting drafts would overstate revenue by every abandoned quote."""
    walk = from_invoices(
        [_invoice(5000, [{"account_id": "1", "item_total": 5000}], status=status)],
        chart,
        PERIOD,
    )
    assert walk.postings == []
    assert walk.skipped[f"invoices:{status}"] == 1


def test_documents_outside_the_period_do_not_post(chart):
    """The period is enforced locally, whatever Zoho did with the filter."""
    outside = _invoice(1000, [{"account_id": "1", "item_total": 1000}])
    outside["date"] = "2026-02-05"
    walk = from_invoices([outside], chart, PERIOD)
    assert walk.postings == []


def test_posting_to_a_balance_sheet_account_is_dropped_silently(chart):
    """Not an orphan: it is the other half of a correct double entry."""
    walk = from_invoices(
        [_invoice(1000, [{"account_id": "10", "item_total": 1000}])], chart, PERIOD
    )
    assert walk.postings == []
    assert walk.orphan_lines == 0


def test_line_with_no_account_is_counted_as_an_orphan(chart):
    walk = from_invoices([_invoice(1000, [{"item_total": 1000}])], chart, PERIOD)
    assert walk.postings == []
    assert walk.orphan_lines == 1


# ── currency and remainders ────────────────────────────────────────────


def test_foreign_invoice_is_converted_at_its_exchange_rate(chart):
    walk = from_invoices(
        [_invoice(100, [{"account_id": "1", "item_total": 100}], rate="3.67")],
        chart,
        PERIOD,
    )
    assert walk.postings[0].amount == money("367.00")


def test_unallocated_records_the_gap_between_total_and_lines(chart):
    """Tax and shipping legitimately sit outside the P&L, and are reported."""
    walk = from_invoices(
        [_invoice(1050, [{"account_id": "1", "item_total": 1000}])], chart, PERIOD
    )
    assert walk.unallocated["invoices"] == money(50)


def test_expense_without_line_items_uses_its_top_level_account(chart):
    """The ordinary, non-itemised expense form."""
    rows = [
        {
            "expense_id": "e-1",
            "date": "2026-01-08",
            "status": "unbilled",
            "account_id": "3",
            "amount": 120,
            "total": 126,
        }
    ]
    walk = from_expenses(rows, chart, PERIOD)
    assert walk.postings[0].amount == money(120), "amount excludes tax; total does not"
    assert walk.unallocated["expenses"] == money(6)


# ── the statement ──────────────────────────────────────────────────────


@pytest.fixture
def statement(chart):
    """A full month, chosen so every subtotal is checkable by hand.

        Income          10,000 invoiced, less a 1,000 credit note  =  9,000
        Cost of sales    4,000 billed                              =  4,000
        Gross profit                                                  5,000
        Operating exp    1,200 rent, less a 200 vendor credit      =  1,000
        Operating profit                                              4,000
        Other income        50 interest                            =     50
        Other expenses      30 bank charges                        =     30
        Net profit                                                    4,020
    """
    walk = Walk()
    walk.extend(
        from_invoices(
            [_invoice(10000, [{"account_id": "1", "item_total": 10000}], day=10)],
            chart,
            PERIOD,
        )
    )
    walk.extend(
        from_credit_notes(
            [
                {
                    "creditnote_id": "cn", "creditnote_number": "CN-9",
                    "date": "2026-01-11", "status": "open", "total": 1000,
                    "line_items": [{"account_id": "1", "item_total": 1000}],
                }
            ],
            chart, PERIOD,
        )
    )
    walk.extend(
        from_bills(
            [
                {
                    "bill_id": "b", "bill_number": "B-9", "date": "2026-01-12",
                    "status": "open", "total": 4000,
                    "line_items": [{"account_id": "2", "item_total": 4000}],
                }
            ],
            chart, PERIOD,
        )
    )
    walk.extend(
        from_expenses(
            [
                {
                    "expense_id": "e", "date": "2026-01-13", "status": "unbilled",
                    "account_id": "3", "amount": 1200, "total": 1200,
                },
                {
                    "expense_id": "e2", "date": "2026-01-14", "status": "unbilled",
                    "account_id": "5", "amount": 30, "total": 30,
                },
            ],
            chart, PERIOD,
        )
    )
    walk.extend(
        from_vendor_credits(
            [
                {
                    "vendor_credit_id": "vc", "vendor_credit_number": "VC-9",
                    "date": "2026-01-15", "status": "open", "total": 200,
                    "line_items": [{"account_id": "3", "item_total": 200}],
                }
            ],
            chart, PERIOD,
        )
    )
    walk.extend(
        from_journals(
            [
                {
                    "journal_id": "j", "entry_number": "J-9",
                    "journal_date": "2026-01-20", "status": "published",
                    "line_items": [
                        {"account_id": "4", "debit_or_credit": "credit", "amount": 50},
                        {"account_id": "9", "debit_or_credit": "debit", "amount": 50},
                    ],
                }
            ],
            chart, PERIOD,
        )
    )
    return build(
        chart, walk, start=PERIOD[0], end=PERIOD[1], currency="AED"
    ), walk


def test_subtotals_build_on_each_other(statement):
    result, _ = statement
    assert result.income == money(9000)
    assert result.cost_of_sales == money(4000)
    assert result.gross_profit == money(5000)
    assert result.operating_expenses == money(1000)
    assert result.operating_profit == money(4000)
    assert result.other_income == money(50)
    assert result.other_expenses == money(30)
    assert result.net_profit == money(4020)


def test_margins_are_percentages_of_revenue(statement):
    result, _ = statement
    assert result.gross_margin == money("55.56")
    assert result.net_margin == money("44.67")


def test_margins_are_none_without_revenue(chart):
    """Not zero — an un-invoiced month is empty, not a collapse."""
    empty = build(chart, Walk(), start=PERIOD[0], end=PERIOD[1], currency="AED")
    assert empty.gross_margin is None
    assert empty.net_margin is None
    assert empty.net_profit == money(0)


def test_every_section_is_present_even_when_empty(chart):
    """A statement must be comparable against last month's, so the shape is fixed."""
    empty = build(chart, Walk(), start=PERIOD[0], end=PERIOD[1], currency="AED")
    assert set(empty.sections) == set(Section)
    assert all(total.accounts == [] for total in empty.sections.values())


def test_a_statement_is_complete_only_when_every_source_was_read(chart):
    result = build(
        chart, Walk(), start=PERIOD[0], end=PERIOD[1], currency="AED",
        missing={"bills": "Bills could not be read"},
    )
    assert result.complete is False


def test_drilldown_finds_the_documents_behind_a_figure(statement):
    _, walk = statement
    postings = drill(walk, "3")
    assert {p.source for p in postings} == {"expenses", "vendorcredits"}
    assert sum((p.amount for p in postings), money(0)) == money(1000)


def test_monthly_buckets_split_by_posting_date(chart):
    walk = Walk()
    walk.extend(
        from_invoices(
            [_invoice(100, [{"account_id": "1", "item_total": 100}], day=5)],
            chart, (date(2026, 1, 1), date(2026, 3, 31)),
        )
    )
    february = _invoice(200, [{"account_id": "1", "item_total": 200}])
    february["date"] = "2026-02-14"
    walk.extend(from_invoices([february], chart, (date(2026, 1, 1), date(2026, 3, 31))))

    buckets = by_month(walk)
    assert list(buckets) == ["2026-01", "2026-02"]
    assert buckets["2026-01"][Section.INCOME] == money(100)
    assert buckets["2026-02"][Section.INCOME] == money(200)


# ── periods ────────────────────────────────────────────────────────────


def test_presets_resolve_against_a_fixed_today():
    today = date(2026, 5, 17)
    this_month = resolve("this_month", None, None, today=today)
    # Dates only: a preset keeps its human label ("This month") where an explicit
    # range is labelled with its dates, and that difference is intended.
    assert (this_month.start, this_month.end) == (date(2026, 5, 1), date(2026, 5, 31))
    assert this_month.label == "This month"
    assert resolve("last_month", None, None, today=today).start == date(2026, 4, 1)
    assert resolve("last_month", None, None, today=today).end == date(2026, 4, 30)
    assert resolve("this_quarter", None, None, today=today).start == date(2026, 4, 1)
    assert resolve("this_quarter", None, None, today=today).end == date(2026, 6, 30)


def test_last_quarter_rolls_back_across_the_year_boundary():
    period = resolve("last_quarter", None, None, today=date(2026, 2, 3))
    assert (period.start, period.end) == (date(2025, 10, 1), date(2025, 12, 31))


def test_last_12_months_ends_at_the_current_month_end():
    period = resolve("last_12_months", None, None, today=date(2026, 5, 17))
    assert (period.start, period.end) == (date(2025, 6, 1), date(2026, 5, 31))


def test_comparison_period_matches_length_rather_than_calendar():
    """31 days must be compared against 31 days, not against February."""
    march = resolve(None, date(2026, 3, 1), date(2026, 3, 31))
    prior = march.previous()
    assert prior.end == date(2026, 2, 28)
    assert (prior.end - prior.start).days == (march.end - march.start).days


def test_a_half_open_range_is_refused():
    with pytest.raises(ValueError, match="both start and end"):
        resolve(None, date(2026, 1, 1), None)


def test_an_inverted_range_is_refused():
    with pytest.raises(ValueError, match="cannot precede"):
        resolve(None, date(2026, 2, 1), date(2026, 1, 1))


def test_an_unknown_preset_names_the_valid_ones():
    with pytest.raises(ValueError, match="this_month"):
        resolve("since_forever", None, None)


# ── the numeric parser ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "0"), ("", "0"), (0, "0"),
        ("1234.56", "1234.56"), (1234.56, "1234.56"),
        ("not a number", "0"), ({}, "0"),
    ],
)
def test_money_survives_everything_zoho_sends(raw, expected):
    assert money(raw) == money(expected)


def test_money_keeps_decimal_exactness():
    """Summed over thousands of lines, binary float error becomes visible."""
    total = sum((money("0.1") for _ in range(10)), money(0))
    assert total == money("1.0")
