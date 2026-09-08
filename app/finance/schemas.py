"""Response shapes for the finance module.

Decimals become floats here and nowhere earlier. Every sum, every subtotal and
every margin is computed in ``Decimal`` and rounded once, half-up, at the point
of presentation — see ``app.finance.pnl``. This module is where the rounded
result crosses into JSON, which has no decimal type, and doing it any sooner
would reintroduce the binary-float error the Decimals exist to avoid.

Dates leave as ``YYYY-MM-DD`` strings, matching ``app.zoho.schemas`` and
``app.proposals.schemas``.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel

from app.finance.accounts import Section
from app.finance.ledger import Posting
from app.finance.pnl import AccountLine, Statement, to_money
from app.finance.service import Period


def _f(value: Decimal | None) -> float | None:
    """A rounded Decimal as a JSON number."""
    return None if value is None else float(to_money(value))


class PeriodOut(BaseModel):
    start: str
    end: str
    label: str

    @classmethod
    def of(cls, period: Period) -> PeriodOut:
        return cls(
            start=period.start.isoformat(),
            end=period.end.isoformat(),
            label=period.label,
        )


class AccountOut(BaseModel):
    """One account's line in the statement."""

    account_id: str
    code: str | None
    name: str
    amount: float
    #: How many postings produced it — a one-posting figure and a
    #: four-hundred-posting figure warrant different levels of suspicion.
    postings: int
    #: The endpoints that contributed, e.g. ``["bills", "journals"]``.
    sources: list[str]

    @classmethod
    def of(cls, line: AccountLine) -> AccountOut:
        return cls(
            account_id=line.account_id,
            code=line.code,
            name=line.name,
            amount=_f(line.amount) or 0.0,
            postings=line.postings,
            sources=sorted(line.sources),
        )


class SectionOut(BaseModel):
    key: str
    label: str
    total: float
    accounts: list[AccountOut]


class IntegrityOut(BaseModel):
    """What the statement could not account for.

    Present on every response rather than only when something is wrong. A P&L
    that silently omits its own caveats is worse than one that shows a zero
    here, because the reader cannot tell the two apart.
    """

    #: True when every source was readable. False means the figures are a floor.
    complete: bool
    #: Sources that could not be read, and what to do about each.
    missing: dict[str, str]
    #: Documents read per source.
    counts: dict[str, int]
    #: Document value that reached no P&L account — tax, shipping, rounding.
    #: Expected to be non-zero; it is the gap against Zoho's own report.
    unallocated: dict[str, float]
    #: Documents excluded because of their status, keyed ``source:status``.
    skipped: dict[str, int]
    #: Lines carrying an amount but naming no account at all.
    orphan_lines: int


class StatementOut(BaseModel):
    """A profit and loss, in the order it is read."""

    period: PeriodOut
    currency: str

    sections: list[SectionOut]

    income: float
    cost_of_sales: float
    gross_profit: float
    operating_expenses: float
    operating_profit: float
    other_income: float
    other_expenses: float
    net_profit: float

    #: Percentages of revenue. ``None`` where there was no revenue to be a
    #: percentage of — not zero, which would read as a collapse.
    gross_margin: float | None
    operating_margin: float | None
    net_margin: float | None

    integrity: IntegrityOut

    @classmethod
    def of(cls, statement: Statement, period: Period) -> StatementOut:
        return cls(
            period=PeriodOut.of(period),
            currency=statement.currency,
            sections=[
                SectionOut(
                    key=section.value,
                    label=section.label,
                    total=_f(statement.sections[section].total) or 0.0,
                    accounts=[
                        AccountOut.of(line)
                        for line in statement.sections[section].accounts
                    ],
                )
                for section in Section
            ],
            income=_f(statement.income) or 0.0,
            cost_of_sales=_f(statement.cost_of_sales) or 0.0,
            gross_profit=_f(statement.gross_profit) or 0.0,
            operating_expenses=_f(statement.operating_expenses) or 0.0,
            operating_profit=_f(statement.operating_profit) or 0.0,
            other_income=_f(statement.other_income) or 0.0,
            other_expenses=_f(statement.other_expenses) or 0.0,
            net_profit=_f(statement.net_profit) or 0.0,
            gross_margin=_f(statement.gross_margin),
            operating_margin=_f(statement.operating_margin),
            net_margin=_f(statement.net_margin),
            integrity=IntegrityOut(
                complete=statement.complete,
                missing=statement.missing,
                counts=statement.counts,
                unallocated={k: _f(v) or 0.0 for k, v in statement.unallocated.items()},
                skipped=statement.skipped,
                orphan_lines=statement.orphan_lines,
            ),
        )


class MovementOut(BaseModel):
    """One figure against its comparison period."""

    key: str
    label: str
    current: float
    previous: float
    change: float
    #: ``None`` when the previous figure was zero: growth from nothing is not a
    #: percentage, and reporting it as 100% or as infinity are both lies.
    change_percent: float | None


class ComparisonOut(BaseModel):
    current: StatementOut
    previous: StatementOut
    movements: list[MovementOut]


class TrendPointOut(BaseModel):
    month: str
    income: float
    cost_of_sales: float
    operating_expenses: float
    net_profit: float


class TrendOut(BaseModel):
    period: PeriodOut
    currency: str
    points: list[TrendPointOut]


class PostingOut(BaseModel):
    """One document behind a figure."""

    date: str
    source: str
    source_id: str
    source_number: str | None
    party: str | None
    debit: float
    credit: float
    amount: float

    @classmethod
    def of(cls, posting: Posting) -> PostingOut:
        return cls(
            date=posting.on.isoformat(),
            source=posting.source,
            source_id=posting.source_id,
            source_number=posting.source_number,
            party=posting.party,
            debit=_f(posting.debit) or 0.0,
            credit=_f(posting.credit) or 0.0,
            amount=_f(posting.amount) or 0.0,
        )


class DrilldownOut(BaseModel):
    """Everything behind one account's figure."""

    period: PeriodOut
    account_id: str
    account_name: str
    section: str
    total: float
    postings: list[PostingOut]


class EndpointStatusOut(BaseModel):
    endpoint: str
    name: str
    path: str
    scope: str
    ok: bool
    verified_in_catalogue: bool
    sample_rows: int | None = None
    reason: str | None = None
    needs_scope: str | None = None


class DiagnosticsOut(BaseModel):
    """What this Zoho token can and cannot read.

    The first thing to open when a statement looks wrong, because the two most
    likely causes — an ungranted OAuth scope and an unverified response key —
    both show up here and nowhere else.
    """

    configured: bool
    organization_id: str
    #: Every scope the full endpoint surface would need.
    all_scopes: list[str]
    #: The shorter list a profit and loss alone needs.
    ledger_scopes: list[str]
    #: Scopes that at least one endpoint was refused for.
    missing_scopes: list[str]
    readable: int
    total: int
    endpoints: list[EndpointStatusOut]
