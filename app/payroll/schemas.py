"""Request and response shapes for payroll.

Money travels as decimal strings, never floats: pydantic writes a ``Decimal``
as a string, and the frontend formats those without turning them into numbers.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

CountryCode = Literal["AE", "IN"]


class PayrollMeOut(BaseModel):
    can_view: bool
    can_process: bool
    can_approve: bool
    can_admin: bool
    via: list[str]


class PersonOut(BaseModel):
    id: uuid.UUID
    display_name: str
    email: str


# ── access ────────────────────────────────────────────────────────────


class MemberIn(BaseModel):
    level: Literal["viewer", "processor"]
    note: str | None = Field(default=None, max_length=500)


class MemberOut(BaseModel):
    user_id: uuid.UUID
    display_name: str
    email: str
    level: str
    note: str | None
    added_by_name: str | None
    created_at: datetime


class RoleHolderOut(BaseModel):
    person: PersonOut
    roles: list[str]


class AccessOverviewOut(BaseModel):
    accounts_team_slug: str
    accounts_team_found: bool
    accounts_members: list[PersonOut]
    role_holders: list[RoleHolderOut]
    members: list[MemberOut]


# ── settings ──────────────────────────────────────────────────────────


class SettingsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    accounts_team_slug: str
    notify_in_app: bool

    ae_company_name: str
    ae_company_address: str | None
    ae_day_basis: str
    ae_hours_per_day: Decimal
    ae_ot_normal_rate: Decimal
    ae_ot_premium_rate: Decimal
    ae_employer_mol_id: str | None
    ae_employer_bank_routing: str | None
    ae_employer_reference: str | None

    in_company_name: str
    in_company_address: str | None
    in_day_basis: str
    in_pf_establishment: str | None
    in_pf_ee_rate: Decimal
    in_pf_er_rate: Decimal
    in_eps_rate: Decimal
    in_pf_ceiling: Decimal
    in_edli_rate: Decimal
    in_pf_admin_rate: Decimal
    in_wage_code_rule: bool
    in_esi_threshold: Decimal
    in_esi_ee_rate: Decimal
    in_esi_er_rate: Decimal
    in_pt_slabs: dict[str, Any]
    in_tax_tables: dict[str, Any]

    invoice_prefix: str
    invoice_payment_terms: int
    invoice_currency: str | None
    invoice_notes: str | None
    invoice_terms: str | None
    invoice_customer_zoho_id: str | None
    mail_enabled: bool

    updated_at: datetime


DayBasis = Literal["fixed_30", "calendar"]
Rate = Field(default=None, ge=0, le=100)


class SettingsIn(BaseModel):
    """Every field optional: only what is sent changes."""

    accounts_team_slug: str | None = Field(default=None, min_length=1, max_length=64)
    notify_in_app: bool | None = None

    ae_company_name: str | None = Field(default=None, min_length=1, max_length=200)
    ae_company_address: str | None = Field(default=None, max_length=1000)
    ae_day_basis: DayBasis | None = None
    ae_hours_per_day: Decimal | None = Field(default=None, gt=0, le=24)
    ae_ot_normal_rate: Decimal | None = Field(default=None, ge=1, le=5)
    ae_ot_premium_rate: Decimal | None = Field(default=None, ge=1, le=5)
    ae_employer_mol_id: str | None = Field(default=None, max_length=20)
    ae_employer_bank_routing: str | None = Field(default=None, max_length=20)
    ae_employer_reference: str | None = Field(default=None, max_length=60)

    in_company_name: str | None = Field(default=None, min_length=1, max_length=200)
    in_company_address: str | None = Field(default=None, max_length=1000)
    in_day_basis: DayBasis | None = None
    in_pf_establishment: str | None = Field(default=None, max_length=40)
    in_pf_ee_rate: Decimal | None = Rate
    in_pf_er_rate: Decimal | None = Rate
    in_eps_rate: Decimal | None = Rate
    in_pf_ceiling: Decimal | None = Field(default=None, ge=0)
    in_edli_rate: Decimal | None = Rate
    in_pf_admin_rate: Decimal | None = Rate
    in_wage_code_rule: bool | None = None
    in_esi_threshold: Decimal | None = Field(default=None, ge=0)
    in_esi_ee_rate: Decimal | None = Rate
    in_esi_er_rate: Decimal | None = Rate
    in_pt_slabs: dict[str, list[dict[str, Any]]] | None = None

    invoice_prefix: str | None = Field(default=None, min_length=1, max_length=20)
    invoice_payment_terms: int | None = Field(default=None, ge=0, le=365)
    invoice_currency: str | None = Field(default=None, min_length=3, max_length=3)
    invoice_notes: str | None = Field(default=None, max_length=2000)
    invoice_terms: str | None = Field(default=None, max_length=4000)
    invoice_customer_zoho_id: str | None = Field(default=None, max_length=40)
    mail_enabled: bool | None = None


class TaxTableIn(BaseModel):
    """One financial year's new-regime table. ``confirm`` stamps who checked it."""

    slabs: list[dict[str, Any]]
    standard_deduction: Decimal = Field(ge=0)
    rebate_limit: Decimal = Field(ge=0)
    rebate_max: Decimal = Field(ge=0)
    cess_rate: Decimal = Field(default=Decimal("4"), ge=0, le=100)
    surcharge: list[dict[str, Any]] = Field(default_factory=list)
    source: str | None = Field(default=None, max_length=300)
    confirm: bool = False


class ComponentOut(BaseModel):
    code: str
    name: str
    countries: list[str]
    repeatable: bool
    basic: bool
    pf_wage: bool


# ── employees ─────────────────────────────────────────────────────────


class EmployeeFields(BaseModel):
    user_id: uuid.UUID | None = None
    code: str = Field(min_length=1, max_length=40)
    full_name: str = Field(min_length=1, max_length=200)
    email: str | None = Field(default=None, max_length=320)
    designation: str | None = Field(default=None, max_length=120)
    department: str | None = Field(default=None, max_length=120)
    joined_on: date
    left_on: date | None = None

    bank_name: str | None = Field(default=None, max_length=120)
    bank_account: str | None = Field(default=None, max_length=40)
    iban: str | None = Field(default=None, max_length=34)
    bank_routing: str | None = Field(default=None, max_length=20)
    mol_person_id: str | None = Field(default=None, max_length=20)
    ifsc: str | None = Field(default=None, max_length=11)
    pan: str | None = Field(default=None, max_length=10)
    uan: str | None = Field(default=None, max_length=12)
    esi_number: str | None = Field(default=None, max_length=17)
    pt_state: str | None = Field(default=None, max_length=4)
    pf_enabled: bool = True
    pf_on_actual: bool = False
    eps_member: bool = True
    esi_mode: Literal["auto", "yes", "no"] = "auto"
    pt_override: Decimal | None = Field(default=None, ge=0)
    tds_monthly_override: Decimal | None = Field(default=None, ge=0)
    opening_fy: int | None = Field(default=None, ge=2000, le=2100)
    opening_taxable: Decimal = Field(default=Decimal("0"), ge=0)
    opening_tds: Decimal = Field(default=Decimal("0"), ge=0)
    notes: str | None = Field(default=None, max_length=4000)


class SalaryIn(BaseModel):
    effective_from: date
    components: list[dict[str, Any]] = Field(min_length=1)
    note: str | None = Field(default=None, max_length=1000)


class EmployeeIn(EmployeeFields):
    country: CountryCode
    #: The first salary structure, so a new employee is payable at once.
    salary: SalaryIn | None = None


class EmployeePatch(BaseModel):
    """Only the fields sent change. Country is fixed once set."""

    user_id: uuid.UUID | None = None
    code: str | None = Field(default=None, min_length=1, max_length=40)
    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    email: str | None = Field(default=None, max_length=320)
    designation: str | None = Field(default=None, max_length=120)
    department: str | None = Field(default=None, max_length=120)
    joined_on: date | None = None
    left_on: date | None = None
    bank_name: str | None = Field(default=None, max_length=120)
    bank_account: str | None = Field(default=None, max_length=40)
    iban: str | None = Field(default=None, max_length=34)
    bank_routing: str | None = Field(default=None, max_length=20)
    mol_person_id: str | None = Field(default=None, max_length=20)
    ifsc: str | None = Field(default=None, max_length=11)
    pan: str | None = Field(default=None, max_length=10)
    uan: str | None = Field(default=None, max_length=12)
    esi_number: str | None = Field(default=None, max_length=17)
    pt_state: str | None = Field(default=None, max_length=4)
    pf_enabled: bool | None = None
    pf_on_actual: bool | None = None
    eps_member: bool | None = None
    esi_mode: Literal["auto", "yes", "no"] | None = None
    pt_override: Decimal | None = Field(default=None, ge=0)
    tds_monthly_override: Decimal | None = Field(default=None, ge=0)
    opening_fy: int | None = Field(default=None, ge=2000, le=2100)
    opening_taxable: Decimal | None = Field(default=None, ge=0)
    opening_tds: Decimal | None = Field(default=None, ge=0)
    notes: str | None = Field(default=None, max_length=4000)


class SalaryOut(BaseModel):
    id: uuid.UUID
    effective_from: date
    components: list[dict[str, Any]]
    monthly_total: Decimal
    note: str | None
    created_at: datetime


class EmployeeOut(EmployeeFields):
    id: uuid.UUID
    country: str
    currency: str
    active: bool
    user_name: str | None
    current_salary: SalaryOut | None
    salaries: list[SalaryOut]
    created_at: datetime
    updated_at: datetime


class GratuityRowOut(BaseModel):
    employee_id: uuid.UUID
    code: str
    full_name: str
    joined_on: date
    left_on: date | None
    basic: Decimal
    service_years: Decimal
    gratuity: Decimal


# ── runs ──────────────────────────────────────────────────────────────


class RunCreateIn(BaseModel):
    country: CountryCode
    year: int = Field(ge=2000, le=2100)
    month: int = Field(ge=1, le=12)
    note: str | None = Field(default=None, max_length=2000)


class LineIn(BaseModel):
    """The month's inputs for one person. Only the fields sent change."""

    lop_adjust: Decimal | None = Field(default=None, ge=-31, le=31)
    ot_normal_hours: Decimal | None = Field(default=None, ge=0, le=400)
    ot_premium_hours: Decimal | None = Field(default=None, ge=0, le=400)
    extras: list[dict[str, Any]] | None = None
    #: Send null to clear an override; leave it out to keep it.
    tds_override: Decimal | None = Field(default=None, ge=0)
    on_hold: bool | None = None
    note: str | None = Field(default=None, max_length=2000)
    #: The employee's joining date, changed from the pay sheet for someone who
    #: started part-way through the month. It is the employee's record that
    #: changes, so every draft month it moves through is reworked.
    joined_on: date | None = None


class LineOut(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    employee_code: str
    employee_name: str
    designation: str | None
    identifiers: dict[str, Any]
    lop_adjust: Decimal
    ot_normal_hours: Decimal
    ot_premium_hours: Decimal
    extras: list[dict[str, Any]]
    tds_override: Decimal | None
    on_hold: bool
    note: str | None
    leave_lop_days: Decimal
    basis_days: Decimal
    paid_days: Decimal
    unpaid_days: Decimal
    earnings: list[dict[str, Any]]
    deductions: list[dict[str, Any]]
    employer: list[dict[str, Any]]
    gross: Decimal
    total_deductions: Decimal
    net: Decimal
    employer_cost: Decimal
    taxable: Decimal
    tds: Decimal
    workings: dict[str, Any]
    warnings: list[str]


class RunOut(BaseModel):
    id: uuid.UUID
    country: str
    year: int
    month: int
    label: str
    currency: str
    status: str
    headcount: int
    gross: Decimal
    deductions: Decimal
    net: Decimal
    employer_cost: Decimal
    warnings: list[str]
    warning_count: int
    note: str | None
    calculated_at: datetime | None
    created_by_name: str | None
    submitted_by_id: uuid.UUID | None
    submitted_by_name: str | None
    submitted_at: datetime | None
    approved_by_name: str | None
    approved_at: datetime | None
    returned_note: str | None
    paid_by_name: str | None
    paid_on: date | None
    created_at: datetime


class EventOut(BaseModel):
    id: uuid.UUID
    action: str
    summary: str
    detail: dict[str, Any]
    actor_name: str | None
    run_id: uuid.UUID | None
    employee_id: uuid.UUID | None
    created_at: datetime


class RunDetailOut(RunOut):
    lines: list[LineOut]
    events: list[EventOut]


class NoteIn(BaseModel):
    note: str | None = Field(default=None, max_length=2000)


class ReturnIn(BaseModel):
    note: str = Field(min_length=1, max_length=2000)


class ReopenIn(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class PaidIn(BaseModel):
    paid_on: date


# ── the month's invoice ───────────────────────────────────────────


class ChargeIn(BaseModel):
    """One charge that is not salary — rent, internet, anything. As Zoho takes it."""

    item_id: str | None = Field(default=None, max_length=40)
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    quantity: Decimal = Field(default=Decimal("1"), gt=0)
    rate: Decimal = Field(ge=0)
    tax_id: str | None = Field(default=None, max_length=40)
    tax_name: str | None = Field(default=None, max_length=100)
    tax_percentage: Decimal = Field(default=Decimal("0"), ge=0, le=100)


class RunInvoiceIn(BaseModel):
    customer_zoho_id: str | None = Field(default=None, max_length=40)
    customer_name: str = Field(min_length=1, max_length=200)
    customer_address: str | None = Field(default=None, max_length=1000)
    currency: str = Field(min_length=3, max_length=3)
    exchange_rate: Decimal | None = Field(default=None, gt=0)
    invoice_date: date
    due_date: date | None = None
    payment_terms: int | None = Field(default=None, ge=0, le=365)
    reference_number: str | None = Field(default=None, max_length=100)
    salary_lines: Literal["single", "per_employee"] = "single"
    charges: list[ChargeIn] = Field(default_factory=list)
    adjustment: Decimal = Decimal("0")
    adjustment_description: str | None = Field(default=None, max_length=100)
    notes: str | None = Field(default=None, max_length=2000)
    terms: str | None = Field(default=None, max_length=4000)


class InvoiceLineOut(BaseModel):
    #: ``salary`` lines are read off the payslips; ``charge`` lines are typed in.
    source: Literal["salary", "charge"]
    item_id: str | None = None
    name: str
    description: str | None = None
    quantity: Decimal
    rate: Decimal
    tax_id: str | None = None
    tax_name: str | None = None
    tax_percentage: Decimal
    amount: Decimal
    tax_amount: Decimal


class RunInvoiceOut(BaseModel):
    """The month's invoice. Before it is first saved, ``id`` and ``number`` are
    empty and every field is the default from payroll settings."""

    id: uuid.UUID | None
    run_id: uuid.UUID
    number: str | None
    issuer_name: str
    issuer_address: str | None
    customer_zoho_id: str | None
    customer_name: str
    customer_address: str | None
    currency: str
    run_currency: str
    #: Payroll-currency units per invoice-currency unit (INR per AED).
    exchange_rate: Decimal | None
    #: The invoice is in another currency and there is no rate to convert at.
    needs_rate: bool
    #: Zoho's rate for the invoice date, and where it came from (or why there is none).
    zoho_rate: Decimal | None
    zoho_rate_note: str | None
    #: The invoice's rate is Zoho's, not one typed in.
    rate_from_zoho: bool
    invoice_date: date
    due_date: date | None
    payment_terms: int | None
    reference_number: str | None
    salary_lines: Literal["single", "per_employee"]
    charges: list[ChargeIn]
    lines: list[InvoiceLineOut]
    adjustment: Decimal
    adjustment_description: str | None
    notes: str | None
    terms: str | None
    salaries: Decimal
    sub_total: Decimal
    tax_total: Decimal
    total: Decimal
    zoho_mode: Literal["test", "live"] | None
    zoho_payload: dict[str, Any] | None
    zoho_invoice_id: str | None
    zoho_invoice_number: str | None
    zoho_at: datetime | None
    zoho_by_name: str | None
    zoho_error: str | None
    report_sent_at: datetime | None
    report_to: list[str]
    report_error: str | None


class ZohoContactOut(BaseModel):
    contact_id: str
    contact_name: str
    company_name: str | None = None
    currency_code: str | None = None
    email: str | None = None
    tax_treatment: str | None = None


class ZohoItemOut(BaseModel):
    item_id: str
    name: str
    description: str | None = None
    rate: Decimal | None = None
    tax_id: str | None = None


class ZohoTaxOut(BaseModel):
    tax_id: str
    tax_name: str
    tax_percentage: Decimal


class InvoiceSetupOut(BaseModel):
    """What the invoice form needs that is not an invoice."""

    #: True only when ``PAYROLL_ZOHO_INVOICE_WRITE`` is on: "Create in Zoho" writes.
    zoho_live: bool
    zoho_organization: str | None
    mail: bool
    #: Why Zoho could not be read, when it could not; the form still works.
    zoho_error: str | None
    taxes: list[ZohoTaxOut]
    currencies: list[str]
    #: A new invoice's currency, and Zoho's rate to it from the India payroll's (INR) today.
    default_currency: str
    default_rate: Decimal | None
    default_rate_note: str | None
    #: Who the approval mail goes to, and who gets the report.
    approvers: list[str]
    report_to: list[str]
