"""Every Zoho Books endpoint this app can read, as data rather than as methods.

Ninety hand-written methods would all say the same thing. Zoho's list endpoints
are uniform to a fault::

    GET /{path}          -> {"<list_key>": [...], "page_context": {...}}
    GET /{path}/{id}     -> {"<detail_key>": {...}}

So the surface is a table, and ``ZohoBooks.list_rows``/``get_row`` walk it.
Adding an endpoint is one entry here, not a method, a test and a docstring.

**This table is read-only by construction.** There is no write column and no code
that could use one. Zoho Books is the system of record for the accounts; this app
reports on it and never posts to it.

Three things the table has to carry, each of which would otherwise cost real
debugging time:

* **The keys are not derivable from the path.** ``/chartofaccounts`` lists under
  ``chartofaccounts`` but returns one record under ``chart_of_account``, and
  ``/basecurrencyadjustment`` returns one under the wholly unrelated ``data``.
  Deriving the singular from the plural is wrong often enough to be useless.
* **The scope is not the module name.** Journals and the chart of accounts both
  need ``ZohoBooks.accountants.READ``; currencies, taxes and locations all need
  ``ZohoBooks.settings.READ``. A caller that gets a 403 needs to be told which
  scope to add, and only this table knows it.
* **Some entries are unverified.** Zoho documents roughly forty-five modules and
  does not publish response keys for all of them. ``verified`` records whether an
  entry was checked against the documentation or inferred from the naming
  pattern, and the client falls back to finding the array itself when an inferred
  key misses — so a wrong guess degrades to "still works" rather than "silently
  empty". Anything ``verified=False`` should be confirmed against the live
  organisation and flipped; ``/finance/diagnostics`` is what confirms it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

#: Scopes named once because several modules share each, and a typo in one of
#: them surfaces as a confusing 403 rather than as a mistake in this file.
ACCOUNTANTS: Final = "ZohoBooks.accountants.READ"
SETTINGS: Final = "ZohoBooks.settings.READ"
BANKING: Final = "ZohoBooks.banking.READ"


@dataclass(frozen=True, slots=True)
class Endpoint:
    #: What a caller names it. Stable; the path is not.
    key: str
    #: Appended to ``/books/v3``. Carries no organisation id — the client adds
    #: ``organization_id`` to every query itself.
    path: str
    #: The array key in a list response.
    list_key: str
    #: The object key in a single-record response. ``None`` where the endpoint
    #: has no by-id form (settings collections, mostly).
    detail_key: str | None
    #: The OAuth scope a refresh token must carry to read this.
    scope: str
    #: Shown when an endpoint 403s, so the message can say what was refused.
    name: str
    #: False where the path or keys were inferred from Zoho's naming pattern
    #: rather than read off the documentation. See the module docstring.
    verified: bool = False
    #: Sub-resources ({parent_id} in the path) cannot be swept without a parent,
    #: so the generic list route refuses them rather than 404ing upstream.
    parameterised: bool = False
    #: Endpoints whose postings a profit and loss is built from.
    ledger: bool = False


ENDPOINTS: Final[tuple[Endpoint, ...]] = (
    # ── the organisation itself ────────────────────────────────────────
    Endpoint(
        "organizations", "/organizations", "organizations", "organization",
        SETTINGS, "Organisations", verified=True,
    ),
    Endpoint(
        "users", "/users", "users", "user",
        "ZohoBooks.users.READ", "Books users",
    ),

    # ── who we trade with ──────────────────────────────────────────────
    Endpoint(
        "contacts", "/contacts", "contacts", "contact",
        "ZohoBooks.contacts.READ", "Customers and vendors", verified=True,
    ),
    Endpoint(
        "contactpersons", "/contacts/{parent_id}/contactpersons", "contact_persons",
        "contact_person", "ZohoBooks.contacts.READ", "Contact people",
        parameterised=True,
    ),

    # ── sales ──────────────────────────────────────────────────────────
    Endpoint(
        "estimates", "/estimates", "estimates", "estimate",
        "ZohoBooks.estimates.READ", "Quotes", verified=True,
    ),
    Endpoint(
        "salesorders", "/salesorders", "salesorders", "salesorder",
        "ZohoBooks.salesorders.READ", "Sales orders", verified=True,
    ),
    Endpoint(
        "salesreceipts", "/salesreceipts", "salesreceipts", "salesreceipt",
        "ZohoBooks.invoices.READ", "Sales receipts",
    ),
    Endpoint(
        "invoices", "/invoices", "invoices", "invoice",
        "ZohoBooks.invoices.READ", "Invoices", verified=True, ledger=True,
    ),
    Endpoint(
        "recurringinvoices", "/recurringinvoices", "recurring_invoices",
        "recurring_invoice", "ZohoBooks.invoices.READ", "Recurring invoices",
    ),
    Endpoint(
        "creditnotes", "/creditnotes", "creditnotes", "creditnote",
        "ZohoBooks.creditnotes.READ", "Credit notes", verified=True, ledger=True,
    ),
    Endpoint(
        "customerpayments", "/customerpayments", "customerpayments", "payment",
        "ZohoBooks.customerpayments.READ", "Customer payments", verified=True,
    ),
    Endpoint(
        "retainerinvoices", "/retainerinvoices", "retainerinvoices",
        "retainerinvoice", "ZohoBooks.invoices.READ", "Retainer invoices",
    ),
    Endpoint(
        "deliverychallans", "/deliverychallans", "deliverychallans",
        "deliverychallan", "ZohoBooks.deliverychallans.READ", "Delivery challans",
        verified=True,
    ),

    # ── purchasing ─────────────────────────────────────────────────────
    Endpoint(
        "purchaseorders", "/purchaseorders", "purchaseorders", "purchaseorder",
        "ZohoBooks.purchaseorders.READ", "Purchase orders", verified=True,
    ),
    Endpoint(
        "bills", "/bills", "bills", "bill",
        "ZohoBooks.bills.READ", "Bills", verified=True, ledger=True,
    ),
    Endpoint(
        "recurringbills", "/recurringbills", "recurring_bills", "recurring_bill",
        "ZohoBooks.bills.READ", "Recurring bills", verified=True,
    ),
    Endpoint(
        "vendorcredits", "/vendorcredits", "vendor_credits", "vendor_credit",
        "ZohoBooks.vendorcredits.READ", "Vendor credits", ledger=True,
    ),
    Endpoint(
        "vendorpayments", "/vendorpayments", "vendorpayments", "vendorpayment",
        "ZohoBooks.vendorpayments.READ", "Vendor payments",
    ),
    Endpoint(
        "expenses", "/expenses", "expenses", "expense",
        "ZohoBooks.expenses.READ", "Expenses", verified=True, ledger=True,
    ),
    Endpoint(
        "recurringexpenses", "/recurringexpenses", "recurring_expenses",
        "recurring_expense", "ZohoBooks.expenses.READ", "Recurring expenses",
    ),

    # ── the ledger proper ──────────────────────────────────────────────
    Endpoint(
        "chartofaccounts", "/chartofaccounts", "chartofaccounts", "chart_of_account",
        ACCOUNTANTS, "Chart of accounts", verified=True,
    ),
    Endpoint(
        "journals", "/journals", "journals", "journal",
        ACCOUNTANTS, "Manual journal entries", verified=True, ledger=True,
    ),
    Endpoint(
        "registers", "/chartofaccounts/{parent_id}/register", "register", None,
        ACCOUNTANTS, "Account register", parameterised=True,
    ),
    Endpoint(
        "basecurrencyadjustment", "/basecurrencyadjustment", "base_currency_adjustments",
        "data", ACCOUNTANTS, "Base currency adjustments", verified=True,
    ),
    Endpoint(
        "openingbalances", "/settings/openingbalances", "opening_balance", None,
        SETTINGS, "Opening balances",
    ),
    Endpoint(
        # Not under /settings, despite being a setting, and scoped to
        # accountants rather than settings. Both were inferred wrongly at first
        # and corrected against the live organisation.
        "transactionlocking", "/transactionlock", "transaction_lock", None,
        ACCOUNTANTS, "Transaction locking", verified=True,
    ),

    # ── banking ────────────────────────────────────────────────────────
    Endpoint(
        "bankaccounts", "/bankaccounts", "bankaccounts", "bankaccount",
        BANKING, "Bank accounts", verified=True,
    ),
    Endpoint(
        "banktransactions", "/banktransactions", "banktransactions", "banktransaction",
        BANKING, "Bank transactions", verified=True,
    ),
    Endpoint(
        "bankrules", "/bankaccounts/rules", "rules", "rule",
        BANKING, "Bank rules",
    ),

    # ── what we sell ───────────────────────────────────────────────────
    Endpoint(
        "items", "/items", "items", "item",
        "ZohoBooks.items.READ", "Items", verified=True,
    ),
    Endpoint(
        "pricebooks", "/pricebooks", "pricebooks", "pricebook",
        SETTINGS, "Price lists", verified=True,
    ),
    Endpoint(
        "fixedassets", "/fixedassets", "fixed_assets", "fixed_asset",
        ACCOUNTANTS, "Fixed assets",
    ),

    # ── delivery ───────────────────────────────────────────────────────
    Endpoint(
        "projects", "/projects", "projects", "project",
        "ZohoBooks.projects.READ", "Projects", verified=True,
    ),
    Endpoint(
        "tasks", "/projects/{parent_id}/tasks", "tasks", "task",
        "ZohoBooks.projects.READ", "Project tasks", parameterised=True,
    ),
    Endpoint(
        "timeentries", "/projects/timeentries", "time_entries", "time_entry",
        "ZohoBooks.projects.READ", "Time entries", verified=True,
    ),

    # ── settings that transactions refer to ────────────────────────────
    Endpoint(
        "currencies", "/settings/currencies", "currencies", "currency",
        SETTINGS, "Currencies", verified=True,
    ),
    Endpoint(
        "taxes", "/settings/taxes", "taxes", "tax",
        SETTINGS, "Taxes", verified=True,
    ),
    Endpoint(
        # /locations, not /settings/locations. Note this 404s rather than 403s
        # on an organisation that has never enabled the Locations feature, which
        # is a different thing from a missing scope and reads that way in the
        # diagnostics output.
        "locations", "/locations", "locations", "location",
        SETTINGS, "Locations", verified=True,
    ),
    Endpoint(
        # /reportingtags, not /settings/tags. Both return 403 on this
        # organisation, so the wrong one could not be caught by probing — it was
        # corrected against the documentation.
        "reportingtags", "/reportingtags", "reporting_tags", "reporting_tag",
        SETTINGS, "Reporting tags", verified=True,
    ),
)

BY_KEY: Final[dict[str, Endpoint]] = {e.key: e for e in ENDPOINTS}

#: Everything a generic sweep can ask for without being handed a parent id.
SWEEPABLE: Final[tuple[Endpoint, ...]] = tuple(
    e for e in ENDPOINTS if not e.parameterised
)

#: The endpoints a computed profit and loss actually reads. Declared here rather
#: than in the finance module so the two cannot drift apart.
LEDGER: Final[tuple[Endpoint, ...]] = tuple(e for e in ENDPOINTS if e.ledger)

#: Every distinct scope the full surface needs — for the diagnostics endpoint,
#: and for whoever has to regenerate the refresh token.
ALL_SCOPES: Final[tuple[str, ...]] = tuple(sorted({e.scope for e in ENDPOINTS}))

#: What a profit and loss alone needs. Considerably shorter than ALL_SCOPES and
#: worth quoting separately: a token can carry only these and still drive the
#: report, which is the narrower grant to ask the Zoho administrator for.
LEDGER_SCOPES: Final[tuple[str, ...]] = tuple(
    sorted({e.scope for e in LEDGER} | {ACCOUNTANTS})
)
