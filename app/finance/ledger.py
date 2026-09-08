"""Turning Zoho documents into ledger postings.

This is the part that has to be right, so it is worth being explicit about what
it does and does not do.

**Why documents rather than a general ledger.** Zoho has no endpoint that serves
the general ledger. ``/journals`` sounds like one and is not — it returns
*manual* journal entries only, and the postings Zoho generates automatically when
an invoice or a bill is raised never appear there. Reading ``/journals`` alone
would produce a P&L containing nothing but hand-typed adjustments, which for most
organisations is close to an empty report. So the postings are reconstructed from
the documents that caused them: invoices, credit notes, bills, vendor credits,
expenses, and then manual journals on top.

**Accrual, not cash.** A document posts on its own date, not on the date it was
paid. An invoice dated in March is March revenue whether or not the money has
arrived. This matches how Zoho's own P&L is normally run and is what "profit"
ordinarily means; a cash-basis report is a different report, not a setting.

**Only one half of each entry is kept.** Every document is a balanced double
entry — an invoice credits income and debits receivables — and only the half
landing on an income or expense account belongs in a P&L. The other half is
dropped by ``Chart.section_of`` returning ``None``, which is why the chart of
accounts has to be loaded before any document is walked.

**Status decides whether a document posts at all.** Drafts have not happened yet
and voids have been undone; both must be excluded or revenue is overstated by
every abandoned draft ever created. Zoho's own report draws the line in the same
place, which matters because these two figures will be compared.

**The period is enforced here, not upstream.** Several of these endpoints take
date filters and several quietly ignore them, so every posting is filtered
against the period locally as well. Upstream filters stay as an optimisation —
they keep the sweep small — but correctness never depends on Zoho having honoured
one.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from app.finance.accounts import Chart, Section

logger = logging.getLogger("hamdaz.finance")

ZERO: Final = Decimal("0")

#: A document in one of these states has not happened, or has been undone.
#: Applied to invoices, credit notes, bills and vendor credits alike.
_NON_POSTING: Final[frozenset[str]] = frozenset({"draft", "void", "voided"})

#: Manual journals post only once published. Zoho's own report agrees.
_JOURNAL_POSTING: Final[frozenset[str]] = frozenset({"published"})


def money(value: Any) -> Decimal:
    """A Decimal from whatever Zoho put in a numeric field.

    Zoho is inconsistent about whether an amount arrives as a number or as a
    string, and a missing amount arrives as ``None``, ``""`` or is simply
    absent. Going through ``str`` rather than ``float`` keeps 0.1 exact, which
    matters once thousands of lines are summed into a figure somebody reconciles
    against Zoho by eye.
    """
    if value is None or value == "":
        return ZERO
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return ZERO


def parse_date(value: Any) -> date | None:
    """Zoho dates are ``YYYY-MM-DD``. Anything else is not a date."""
    if not value or not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class Posting:
    """One side of one entry, landing on one P&L account."""

    account_id: str
    section: Section
    debit: Decimal
    credit: Decimal
    on: date
    #: Which endpoint this came from — "invoices", "bills", "journals" and so
    #: on. Carried through to the drill-down so a figure can be traced back.
    source: str
    source_id: str
    source_number: str | None
    party: str | None

    @property
    def amount(self) -> Decimal:
        """The figure as it reads in its section, sign already applied."""
        return self.section.signed(self.debit, self.credit)


@dataclass(slots=True)
class Walk:
    """Everything one sweep produced, including what it could not place.

    ``unallocated`` is the reason this type exists. A document total rarely
    equals the sum of its P&L postings — tax, shipping and rounding land on
    balance sheet accounts, and a line with no ``account_id`` cannot be placed at
    all. Discarding that difference silently is how a computed P&L quietly stops
    matching the books. Recording it means ``/finance/reconciliation`` can show
    the gap and say which documents caused it.
    """

    postings: list[Posting] = field(default_factory=list)
    #: Document totals by source, for the reconciliation.
    document_totals: dict[str, Decimal] = field(default_factory=dict)
    #: Amounts on posting documents that reached no P&L account.
    unallocated: dict[str, Decimal] = field(default_factory=dict)
    #: Documents skipped for status, by source and status.
    skipped: dict[str, int] = field(default_factory=dict)
    #: Lines that carried an amount but named no account at all.
    orphan_lines: int = 0

    def extend(self, other: Walk) -> None:
        self.postings.extend(other.postings)
        for bucket, value in other.document_totals.items():
            self.document_totals[bucket] = self.document_totals.get(bucket, ZERO) + value
        for bucket, value in other.unallocated.items():
            self.unallocated[bucket] = self.unallocated.get(bucket, ZERO) + value
        for bucket, count in other.skipped.items():
            self.skipped[bucket] = self.skipped.get(bucket, 0) + count
        self.orphan_lines += other.orphan_lines


def _rate(row: dict) -> Decimal:
    """The multiplier from a document's currency into the base currency.

    Zoho reports ``exchange_rate`` on every document and sets it to 1 for the
    base currency, so this is unconditional rather than a multi-currency special
    case. A missing or zero rate is treated as 1: reporting a foreign invoice at
    face value is wrong, but reporting it as nothing is worse and harder to spot.
    """
    rate = money(row.get("exchange_rate"))
    return rate if rate > ZERO else Decimal("1")


def _posts(row: dict, source: str, walk: Walk) -> bool:
    """Whether this document posts, recording it in ``skipped`` if not."""
    status = (row.get("status") or "").strip().casefold()
    if status in _NON_POSTING:
        key = f"{source}:{status}"
        walk.skipped[key] = walk.skipped.get(key, 0) + 1
        return False
    return True


def _line_amount(line: dict) -> Decimal:
    """What a line contributes, net of discount and excluding tax.

    ``item_total`` is Zoho's own net-of-discount figure and is the right one
    where it exists. Falling back to ``rate * quantity`` rather than to ``total``
    is deliberate: on a tax-inclusive document ``total`` contains the tax, which
    belongs on a tax account and not in the P&L line.
    """
    if (item_total := line.get("item_total")) not in (None, ""):
        return money(item_total)
    rate, quantity = money(line.get("rate")), money(line.get("quantity"))
    if quantity == ZERO and rate != ZERO:
        # A line priced without a quantity is a one-off charge, not nothing.
        return rate
    return rate * quantity


def _walk_lines(
    rows: Iterable[dict],
    *,
    chart: Chart,
    source: str,
    id_field: str,
    date_field: str,
    number_field: str,
    party_field: str,
    debit: bool,
    period: tuple[date, date],
    total_field: str = "total",
) -> Walk:
    """The shared shape of every line-item document.

    Invoices, credit notes, bills and vendor credits differ in exactly two ways:
    which side of the ledger they post to, and what their fields are called.
    Everything else — status filtering, the period check, the exchange rate, the
    unallocated remainder — is identical, and writing it four times is how the
    four slowly stop agreeing with each other.
    """
    walk = Walk()
    start, end = period

    for row in rows:
        if not _posts(row, source, walk):
            continue

        on = parse_date(row.get(date_field))
        if on is None or not (start <= on <= end):
            continue

        rate = _rate(row)
        # Named per document rather than derived from ``source``: Zoho spells
        # this vendor_credit_id but calls the module vendorcredits, so stripping
        # the plural would silently produce an empty id and break the drill-down.
        record_id = str(row.get(id_field) or "")
        number = row.get(number_field) or None
        party = row.get(party_field) or None

        document_total = money(row.get(total_field)) * rate
        walk.document_totals[source] = walk.document_totals.get(source, ZERO) + document_total

        allocated = ZERO
        for line in row.get("line_items") or []:
            amount = _line_amount(line) * rate
            if amount == ZERO:
                continue

            account_id = line.get("account_id")
            section = chart.section_of(account_id)
            if section is None:
                # Either a balance sheet account — the other half of the entry,
                # correctly ignored — or a line with no account at all, which is
                # a real gap worth counting.
                if not account_id:
                    walk.orphan_lines += 1
                continue

            allocated += amount
            walk.postings.append(
                Posting(
                    account_id=str(account_id),
                    section=section,
                    debit=amount if debit else ZERO,
                    credit=ZERO if debit else amount,
                    on=on,
                    source=source,
                    source_id=record_id,
                    source_number=number,
                    party=party,
                )
            )

        # Tax, shipping and rounding legitimately live outside the P&L, so this
        # is not an error — but it is the number that explains any difference
        # against Zoho's own report, so it is kept rather than discarded.
        walk.unallocated[source] = (
            walk.unallocated.get(source, ZERO) + (document_total - allocated)
        )

    return walk


def from_invoices(rows: list[dict], chart: Chart, period: tuple[date, date]) -> Walk:
    """Invoices credit income."""
    return _walk_lines(
        rows, chart=chart, source="invoices", id_field="invoice_id",
        date_field="date", number_field="invoice_number",
        party_field="customer_name", debit=False, period=period,
    )


def from_credit_notes(rows: list[dict], chart: Chart, period: tuple[date, date]) -> Walk:
    """Credit notes debit income, reducing revenue.

    The same machinery as an invoice with the side reversed, which is exactly
    what a credit note is.
    """
    return _walk_lines(
        rows, chart=chart, source="creditnotes", id_field="creditnote_id",
        date_field="date", number_field="creditnote_number",
        party_field="customer_name", debit=True, period=period,
    )


def from_bills(rows: list[dict], chart: Chart, period: tuple[date, date]) -> Walk:
    """Bills debit expenses and cost of sales."""
    return _walk_lines(
        rows, chart=chart, source="bills", id_field="bill_id",
        date_field="date", number_field="bill_number",
        party_field="vendor_name", debit=True, period=period,
    )


def from_vendor_credits(rows: list[dict], chart: Chart, period: tuple[date, date]) -> Walk:
    """Vendor credits *credit* an expense account, reducing cost.

    ``debit=False`` is the whole point of this function and is easy to get
    backwards, because every other purchase-side document here debits. A vendor
    credit is a refund from a supplier: getting the side wrong does not merely
    lose the credit, it adds the amount to costs instead of subtracting it, so
    the error lands at twice the value of the credit and in the wrong direction.
    """
    return _walk_lines(
        rows, chart=chart, source="vendorcredits", id_field="vendor_credit_id",
        date_field="date", number_field="vendor_credit_number",
        party_field="vendor_name", debit=False, period=period,
    )


def from_expenses(rows: list[dict], chart: Chart, period: tuple[date, date]) -> Walk:
    """Expenses debit an expense account.

    Shaped unlike the documents above: an ordinary expense names its account at
    the top level rather than in lines, and only an itemised one has
    ``line_items``. Both forms occur in the same organisation, so both are
    handled rather than assuming whichever was seen first.

    ``amount`` rather than ``total`` because ``total`` includes the tax, which
    posts to a tax account.
    """
    walk = Walk()
    start, end = period

    for row in rows:
        if not _posts(row, "expenses", walk):
            continue

        on = parse_date(row.get("date"))
        if on is None or not (start <= on <= end):
            continue

        rate = _rate(row)
        record_id = str(row.get("expense_id") or "")
        party = row.get("vendor_name") or row.get("paid_through_account_name") or None
        reference = row.get("reference_number") or None

        document_total = money(row.get("total")) * rate
        walk.document_totals["expenses"] = (
            walk.document_totals.get("expenses", ZERO) + document_total
        )

        lines = row.get("line_items") or []
        if not lines:
            # The ordinary, non-itemised form: the whole expense on one account.
            lines = [{"account_id": row.get("account_id"), "item_total": row.get("amount")}]

        allocated = ZERO
        for line in lines:
            amount = _line_amount(line) * rate
            if amount == ZERO:
                continue

            account_id = line.get("account_id")
            section = chart.section_of(account_id)
            if section is None:
                if not account_id:
                    walk.orphan_lines += 1
                continue

            allocated += amount
            walk.postings.append(
                Posting(
                    account_id=str(account_id), section=section,
                    debit=amount, credit=ZERO, on=on, source="expenses",
                    source_id=record_id, source_number=reference, party=party,
                )
            )

        walk.unallocated["expenses"] = (
            walk.unallocated.get("expenses", ZERO) + (document_total - allocated)
        )

    return walk


def from_journals(rows: list[dict], chart: Chart, period: tuple[date, date]) -> Walk:
    """Manual journal entries, which state their own debits and credits.

    The only source here that needs no interpretation: a journal line already
    says which account and which side. Unpublished journals are excluded — a
    draft journal has not been posted to the books.

    No unallocated figure is recorded. A journal is balanced across the whole
    entry rather than per line, and most journals touch a balance sheet account
    on one side by design, so the "remainder" that is meaningful for an invoice
    would be noise here.
    """
    walk = Walk()
    start, end = period

    for row in rows:
        status = (row.get("status") or "published").strip().casefold()
        if status not in _JOURNAL_POSTING:
            key = f"journals:{status}"
            walk.skipped[key] = walk.skipped.get(key, 0) + 1
            continue

        on = parse_date(row.get("journal_date") or row.get("date"))
        if on is None or not (start <= on <= end):
            continue

        rate = _rate(row)
        record_id = str(row.get("journal_id") or "")
        number = row.get("entry_number") or row.get("journal_number") or None

        for line in row.get("line_items") or []:
            amount = money(line.get("amount")) * rate
            if amount == ZERO:
                continue

            account_id = line.get("account_id")
            section = chart.section_of(account_id)
            if section is None:
                if not account_id:
                    walk.orphan_lines += 1
                continue

            is_debit = (line.get("debit_or_credit") or "").strip().casefold() == "debit"
            walk.postings.append(
                Posting(
                    account_id=str(account_id), section=section,
                    debit=amount if is_debit else ZERO,
                    credit=ZERO if is_debit else amount,
                    on=on, source="journals", source_id=record_id,
                    source_number=number,
                    party=row.get("reference_number") or None,
                )
            )

    return walk


#: Which walker reads which endpoint. The P&L builder iterates this rather than
#: calling six functions in a fixed order, so adding a seventh source is one
#: entry and cannot be half-wired.
WALKERS: Final[dict[str, Any]] = {
    "invoices": from_invoices,
    "creditnotes": from_credit_notes,
    "bills": from_bills,
    "vendorcredits": from_vendor_credits,
    "expenses": from_expenses,
    "journals": from_journals,
}
