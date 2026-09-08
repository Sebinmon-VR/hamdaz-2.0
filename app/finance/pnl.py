"""Assembling postings into a profit and loss statement.

Nothing here talks to Zoho. It takes a chart of accounts and a bag of postings
and produces the statement, which makes the whole of the arithmetic testable
without a network call — the part most worth testing, since a wrong subtotal is
invisible until somebody reconciles by hand.

The shape is the conventional one, and the order matters because each subtotal
feeds the next::

    Operating income
  - Cost of sales
  = Gross profit
  - Operating expenses
  = Operating profit
  + Other income
  - Other expenses
  = Net profit

Amounts are rounded once, here, at the point of presentation. Summing rounded
figures is how a statement ends up with subtotals that do not add up: round
after totalling, never before.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from app.finance.accounts import Chart, Section
from app.finance.ledger import ZERO, Posting, Walk

#: Two places, half-up. Half-up rather than Python's default half-even because
#: it is what every accounting package does, and a statement that rounds .005
#: differently from Zoho invites a reconciliation hunt over one cent.
_CENTS: Final = Decimal("0.01")


def to_money(value: Decimal) -> Decimal:
    return value.quantize(_CENTS, rounding=ROUND_HALF_UP)


@dataclass(slots=True)
class AccountLine:
    """One account's contribution to the statement."""

    account_id: str
    code: str | None
    name: str
    section: Section
    debit: Decimal = ZERO
    credit: Decimal = ZERO
    postings: int = 0
    #: Which endpoints contributed, so a surprising figure can be traced
    #: without opening the drill-down.
    sources: set[str] = field(default_factory=set)

    @property
    def amount(self) -> Decimal:
        return self.section.signed(self.debit, self.credit)


@dataclass(slots=True)
class SectionTotal:
    section: Section
    accounts: list[AccountLine] = field(default_factory=list)

    @property
    def total(self) -> Decimal:
        return sum((line.amount for line in self.accounts), ZERO)


@dataclass(slots=True)
class Statement:
    """A finished profit and loss for one period."""

    start: date
    end: date
    currency: str
    sections: dict[Section, SectionTotal]
    #: Documents read, by source. Shown so a reader can see the report was built
    #: from something, and spot a source that silently returned nothing.
    counts: dict[str, int] = field(default_factory=dict)
    #: Amounts on posting documents that reached no P&L account — tax, shipping
    #: and rounding, mostly. See ``Walk.unallocated``.
    unallocated: dict[str, Decimal] = field(default_factory=dict)
    #: Documents excluded for status, keyed ``source:status``.
    skipped: dict[str, int] = field(default_factory=dict)
    orphan_lines: int = 0
    #: Sources that could not be read at all. A statement with one of these is
    #: incomplete and says so rather than quietly under-reporting.
    missing: dict[str, str] = field(default_factory=dict)

    # ── the subtotals, each built on the one above ─────────────────────

    def total(self, section: Section) -> Decimal:
        return self.sections[section].total

    @property
    def income(self) -> Decimal:
        return self.total(Section.INCOME)

    @property
    def cost_of_sales(self) -> Decimal:
        return self.total(Section.COST_OF_SALES)

    @property
    def gross_profit(self) -> Decimal:
        return self.income - self.cost_of_sales

    @property
    def operating_expenses(self) -> Decimal:
        return self.total(Section.OPERATING_EXPENSES)

    @property
    def operating_profit(self) -> Decimal:
        return self.gross_profit - self.operating_expenses

    @property
    def other_income(self) -> Decimal:
        return self.total(Section.OTHER_INCOME)

    @property
    def other_expenses(self) -> Decimal:
        return self.total(Section.OTHER_EXPENSES)

    @property
    def net_profit(self) -> Decimal:
        return self.operating_profit + self.other_income - self.other_expenses

    @property
    def complete(self) -> bool:
        """Whether every source was readable.

        False means at least one endpoint was refused or failed, so the figures
        are a floor rather than the answer. Callers surface this rather than
        presenting an under-reported profit as fact.
        """
        return not self.missing

    # ── ratios ─────────────────────────────────────────────────────────

    def _margin(self, value: Decimal) -> Decimal | None:
        """A percentage of revenue, or ``None`` when there is no revenue.

        Guarded rather than defaulted to zero: with no income there is no
        margin, and showing 0% for a month that has not been invoiced yet reads
        as a catastrophe rather than as an empty period.
        """
        if self.income == ZERO:
            return None
        return to_money(value / self.income * Decimal("100"))

    @property
    def gross_margin(self) -> Decimal | None:
        return self._margin(self.gross_profit)

    @property
    def operating_margin(self) -> Decimal | None:
        return self._margin(self.operating_profit)

    @property
    def net_margin(self) -> Decimal | None:
        return self._margin(self.net_profit)


def build(
    chart: Chart,
    walk: Walk,
    *,
    start: date,
    end: date,
    currency: str,
    counts: dict[str, int] | None = None,
    missing: dict[str, str] | None = None,
) -> Statement:
    """Fold postings into a statement.

    Every section is present even when empty. A P&L with no "Other income"
    heading is not a shorter report, it is one a reader cannot compare against
    last month's, so the sections are fixed and the accounts within them vary.
    """
    sections = {section: SectionTotal(section=section) for section in Section}
    lines: dict[str, AccountLine] = {}

    for posting in walk.postings:
        line = lines.get(posting.account_id)
        if line is None:
            account = chart.by_id.get(posting.account_id)
            line = AccountLine(
                account_id=posting.account_id,
                code=account.code if account else None,
                # Only reachable if the chart changed under a cached sweep;
                # naming the id beats showing a blank row.
                name=account.name if account else f"Account {posting.account_id}",
                section=posting.section,
            )
            lines[posting.account_id] = line
            sections[posting.section].accounts.append(line)

        line.debit += posting.debit
        line.credit += posting.credit
        line.postings += 1
        line.sources.add(posting.source)

    # Largest first: on a real chart of accounts the interesting lines are the
    # big ones, and alphabetical order buries them among dormant accounts.
    for total in sections.values():
        total.accounts.sort(key=lambda line: line.amount, reverse=True)

    return Statement(
        start=start,
        end=end,
        currency=currency,
        sections=sections,
        counts=dict(counts or {}),
        unallocated=dict(walk.unallocated),
        skipped=dict(walk.skipped),
        orphan_lines=walk.orphan_lines,
        missing=dict(missing or {}),
    )


def by_month(walk: Walk) -> dict[str, dict[Section, Decimal]]:
    """The same postings cut by calendar month, for a trend line.

    Built from the postings already in hand rather than by running the sweep
    once per month, which would multiply the upstream calls by twelve and hit
    Zoho's rate limit for a chart nobody asked to be authoritative.
    """
    buckets: dict[str, dict[Section, Decimal]] = defaultdict(
        lambda: dict.fromkeys(Section, ZERO)
    )
    for posting in walk.postings:
        key = f"{posting.on.year:04d}-{posting.on.month:02d}"
        buckets[key][posting.section] += posting.amount
    return dict(sorted(buckets.items()))


def drill(walk: Walk, account_id: str) -> list[Posting]:
    """Every posting behind one account's figure, newest first.

    This is what makes a computed statement defensible rather than merely
    plausible: a manager who doubts a number can see the documents that made it.
    """
    return sorted(
        (p for p in walk.postings if p.account_id == account_id),
        key=lambda p: p.on,
        reverse=True,
    )
