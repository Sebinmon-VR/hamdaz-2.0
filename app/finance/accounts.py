"""The chart of accounts, and which part of a profit and loss each account is.

A P&L has a fixed shape, and an account's place in it is decided entirely by its
``account_type`` in Zoho. That mapping is the whole of this module::

    income              -> Operating income
    cost_of_goods_sold  -> Cost of sales
    expense             -> Operating expenses
    other_income        -> Other income
    other_expense       -> Other expenses

Every other type — ``bank``, ``fixed_asset``, ``accounts_receivable``,
``equity`` and the rest — is a balance sheet account and takes no part in a P&L.
They are not an error and not a gap: an invoice debits receivables and credits
income, and only the second half belongs here. ``SECTIONS`` returning ``None``
for them is how the ledger walker knows to drop that half of the posting.

**Sign convention.** Every amount in this module is stored as debit and credit
exactly as it was posted, and turned into a readable figure only at the end, by
``Section.signed``. Income accounts carry credit balances and expense accounts
carry debit balances, so a single "amount" column would need a sign flip
somewhere, and the one thing worse than flipping it is flipping it twice. Keeping
debits and credits until the last moment means the flip happens in exactly one
place and is visible there.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Final


class Section(StrEnum):
    """The bands of a profit and loss, in the order they are read."""

    INCOME = "income"
    COST_OF_SALES = "cost_of_sales"
    OPERATING_EXPENSES = "operating_expenses"
    OTHER_INCOME = "other_income"
    OTHER_EXPENSES = "other_expenses"

    @property
    def label(self) -> str:
        return _LABELS[self]

    @property
    def credit_positive(self) -> bool:
        """Whether a credit balance is a *positive* figure in this section.

        True for the two income bands, false for the three cost bands. This is
        the only place the debit/credit convention becomes a plus or a minus.
        """
        return self in (Section.INCOME, Section.OTHER_INCOME)

    def signed(self, debit: Decimal, credit: Decimal) -> Decimal:
        """The figure as it should be *read*, from the raw postings.

        Income of 100 is a credit of 100 and reads as 100. An expense of 40 is a
        debit of 40 and also reads as 40 — a P&L shows costs as positive numbers
        and subtracts them, rather than showing them negative and adding them.
        A credit note debiting income 20 correctly reads as -20 here, reducing
        revenue, which is what makes refunds work without a special case.
        """
        return credit - debit if self.credit_positive else debit - credit


_LABELS: Final[dict[Section, str]] = {
    Section.INCOME: "Operating income",
    Section.COST_OF_SALES: "Cost of sales",
    Section.OPERATING_EXPENSES: "Operating expenses",
    Section.OTHER_INCOME: "Other income",
    Section.OTHER_EXPENSES: "Other expenses",
}

#: Zoho's ``account_type`` to the band it belongs to. Anything absent is a
#: balance sheet account and is deliberately not represented.
SECTIONS: Final[dict[str, Section]] = {
    "income": Section.INCOME,
    "other_income": Section.OTHER_INCOME,
    "cost_of_goods_sold": Section.COST_OF_SALES,
    "expense": Section.OPERATING_EXPENSES,
    "other_expense": Section.OTHER_EXPENSES,
}


@dataclass(frozen=True, slots=True)
class Account:
    """One chart-of-accounts row, reduced to what a P&L needs."""

    id: str
    name: str
    code: str | None
    account_type: str
    section: Section
    #: Zoho's own grouping within a type, kept for sub-totalling where the
    #: organisation uses it. Often empty.
    group: str | None = None

    @classmethod
    def from_zoho(cls, row: dict) -> Account | None:
        """Build one, or ``None`` if this account has no place in a P&L."""
        account_type = (row.get("account_type") or "").strip().casefold()
        section = SECTIONS.get(account_type)
        if section is None:
            return None

        account_id = row.get("account_id")
        if not account_id:
            return None

        return cls(
            id=str(account_id),
            name=row.get("account_name") or "(unnamed account)",
            code=(row.get("account_code") or "").strip() or None,
            account_type=account_type,
            section=section,
            group=(row.get("account_group") or "").strip() or None,
        )


@dataclass(slots=True)
class Chart:
    """The P&L-relevant accounts, indexed for the ledger walk.

    Only income and expense accounts are kept. A posting to anything else is not
    a problem to report — it is the other half of a double entry, and dropping it
    silently is correct.
    """

    by_id: dict[str, Account] = field(default_factory=dict)
    #: How many chart rows were read in total, including balance sheet ones.
    total_accounts: int = 0

    @classmethod
    def from_zoho(cls, rows: list[dict]) -> Chart:
        chart = cls(total_accounts=len(rows))
        for row in rows:
            account = Account.from_zoho(row)
            if account is not None:
                chart.by_id[account.id] = account
        return chart

    def section_of(self, account_id: str | None) -> Section | None:
        account = self.by_id.get(str(account_id)) if account_id else None
        return account.section if account else None

    def __len__(self) -> int:
        return len(self.by_id)
