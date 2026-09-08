"""The Zoho endpoint catalogue and the finance access model.

Structural tests, all of them cheap and none of them touching Zoho or the
database. They guard the two mistakes this design makes easy: adding a ledger
endpoint without teaching the walker about it, which silently under-reports
profit, and widening who can read the accounts by editing the wrong frozenset.
"""

from __future__ import annotations

import pytest

from app.finance.accounts import SECTIONS, Section
from app.finance.ledger import WALKERS
from app.roles.catalogue import ADMIN_ROLES, FINANCE_ROLES, SYSTEM_ROLES
from app.zoho.catalogue import (
    ALL_SCOPES,
    BY_KEY,
    ENDPOINTS,
    LEDGER,
    LEDGER_SCOPES,
    SWEEPABLE,
)

# ── the endpoint catalogue ─────────────────────────────────────────────


def test_endpoint_keys_are_unique() -> None:
    """A duplicate key would shadow an endpoint in BY_KEY without any error."""
    keys = [e.key for e in ENDPOINTS]
    assert len(keys) == len(set(keys))
    assert len(BY_KEY) == len(ENDPOINTS)


def test_every_ledger_endpoint_has_a_walker() -> None:
    """The failure this guards against is silent and expensive.

    Marking an endpoint ``ledger=True`` without adding a rule to ``WALKERS``
    would sweep the rows, find no way to post them, and produce a statement that
    is short by exactly that source — with no error anywhere.
    """
    assert {e.key for e in LEDGER} == set(WALKERS)


def test_every_walker_has_a_ledger_endpoint() -> None:
    """The reverse: a walker for an endpoint nothing ever sweeps is dead code."""
    assert set(WALKERS) <= {e.key for e in ENDPOINTS}
    assert set(WALKERS) == {e.key for e in ENDPOINTS if e.ledger}


def test_parameterised_endpoints_are_excluded_from_sweeps() -> None:
    """A sub-resource path cannot be swept — it has an unfilled {parent_id}."""
    assert all("{parent_id}" not in e.path for e in SWEEPABLE)
    assert all("{parent_id}" in e.path for e in ENDPOINTS if e.parameterised)


def test_paths_start_with_a_slash_and_carry_no_organisation_id() -> None:
    """The client appends organization_id itself; a path carrying one would double it."""
    for endpoint in ENDPOINTS:
        assert endpoint.path.startswith("/"), endpoint.key
        assert "organization_id" not in endpoint.path, endpoint.key


def test_every_endpoint_declares_a_read_scope() -> None:
    """This module reads. A CREATE or UPDATE scope here would be a bug of intent."""
    for endpoint in ENDPOINTS:
        assert endpoint.scope.endswith(".READ"), endpoint.key
        assert endpoint.scope.startswith("ZohoBooks."), endpoint.key


def test_ledger_scopes_are_a_subset_of_all_scopes() -> None:
    """The narrower grant to ask for must actually be narrower."""
    assert set(LEDGER_SCOPES) <= set(ALL_SCOPES)
    assert len(LEDGER_SCOPES) < len(ALL_SCOPES)


def test_the_chart_of_accounts_scope_is_included_in_the_ledger_grant() -> None:
    """Without the chart, no posting can be classified and every figure is zero."""
    assert BY_KEY["chartofaccounts"].scope in LEDGER_SCOPES


def test_detail_keys_are_declared_where_they_differ_from_the_path() -> None:
    """The irregular ones, checked against Zoho's documentation.

    These are exactly the cases where deriving the singular from the plural
    would be wrong, which is why the catalogue carries them explicitly.
    """
    assert BY_KEY["chartofaccounts"].detail_key == "chart_of_account"
    assert BY_KEY["basecurrencyadjustment"].detail_key == "data"
    assert BY_KEY["customerpayments"].detail_key == "payment"
    assert BY_KEY["currencies"].path == "/settings/currencies"


# ── the P&L sections ───────────────────────────────────────────────────


def test_only_income_and_expense_types_map_to_a_section() -> None:
    balance_sheet = (
        "bank", "cash", "fixed_asset", "other_asset", "other_current_asset",
        "accounts_receivable", "accounts_payable", "equity", "credit_card",
        "long_term_liability", "other_current_liability",
    )
    assert all(account_type not in SECTIONS for account_type in balance_sheet)
    assert set(SECTIONS.values()) == set(Section)


@pytest.mark.parametrize(
    ("section", "credit_positive"),
    [
        (Section.INCOME, True),
        (Section.OTHER_INCOME, True),
        (Section.COST_OF_SALES, False),
        (Section.OPERATING_EXPENSES, False),
        (Section.OTHER_EXPENSES, False),
    ],
)
def test_sign_convention_is_stated_per_section(section, credit_positive) -> None:
    """The single place the debit/credit convention becomes a plus or a minus."""
    assert section.credit_positive is credit_positive


# ── who may read the accounts ──────────────────────────────────────────


def test_finance_roles_all_exist_in_the_catalogue() -> None:
    """A typo here would silently lock everyone out — the guard would never match."""
    assert {r.key for r in SYSTEM_ROLES} >= FINANCE_ROLES


def test_the_accounts_team_gets_finance_without_getting_administration() -> None:
    """The reason FINANCE_ROLES is a separate set rather than a reuse.

    If reading the P&L required an admin role, the only way to give the Accounts
    team their own reports would be to make them Managers — which would also let
    them create teams and grant roles. The whole point of the separate set is
    that ``accountant`` reads the accounts and confers nothing else.
    """
    assert "accountant" in FINANCE_ROLES
    assert "accountant" not in ADMIN_ROLES


def test_admins_and_the_ceo_can_read_the_accounts() -> None:
    assert ADMIN_ROLES <= FINANCE_ROLES


def test_ordinary_team_roles_cannot_read_the_accounts() -> None:
    """The refusal that matters: a P&L is not open to every signed-in user."""
    for key in ("member", "team_lead", "approver", "team_manager"):
        assert key not in FINANCE_ROLES
