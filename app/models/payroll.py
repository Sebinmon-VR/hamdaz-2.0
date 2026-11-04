"""Payroll: who is paid, what they are paid, and each month's run.

Two countries run through one module — the UAE and India — and every employee
belongs to exactly one of them. The country decides the currency and the rules
the month is calculated under (``app/payroll/rules.py``); nothing here converts
between AED and INR, and a run never mixes the two.

**Who may see any of this** is decided in ``app/payroll/access.py``, not by a
team grant. ``PayrollMember`` is the list a super admin keeps there: the people
allowed to read payroll who are not on the Accounts team, and the narrower set
allowed to *process* it.

**A run line is a snapshot.** Once a month is approved its lines are what was
paid, and changing an employee's salary, bank account or name afterwards must
not rewrite them. So a line carries its own copy of everything a payslip or a
bank file needs, and only a draft run is ever recalculated.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Timestamped, UUIDPrimaryKey
from app.models.user import User

#: Money columns. Fourteen digits before the point covers an INR payroll of a
#: few thousand people with room left; two after it is what banks take.
Money = Numeric(16, 2)


class Country(StrEnum):
    UAE = "AE"
    INDIA = "IN"


CURRENCY: dict[str, str] = {Country.UAE: "AED", Country.INDIA: "INR"}


class AccessLevel(StrEnum):
    #: Reads payroll: runs, payslips, the register, employees.
    VIEWER = "viewer"
    #: Also edits it: employees, salaries, the month's inputs, submitting.
    PROCESSOR = "processor"


class RunStatus(StrEnum):
    #: Being prepared. The only state in which anything is recalculated.
    DRAFT = "draft"
    #: Sent for approval. Locked to the processor; an approver decides.
    SUBMITTED = "submitted"
    #: Approved. Bank files can be produced; the figures are final.
    APPROVED = "approved"
    #: The money has gone out.
    PAID = "paid"


class InvoiceStatus(StrEnum):
    #: Being prepared by the accountant.
    DRAFT = "draft"
    #: Sent to the super admins to check. Locked until they decide.
    SUBMITTED = "submitted"
    #: Approved. From here it goes to Zoho, and the report goes out.
    APPROVED = "approved"


class PayrollSettings(Base, Timestamped):
    """The rules and identifiers payroll is calculated with. One row, ``id`` 1.

    Rates are stored as percentages (``12.00`` for 12 %) because that is how
    every statutory notice states them, and a processor checking a figure
    against a circular should not have to move a decimal point to do it.
    """

    __tablename__ = "payroll_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)

    #: Whose members may read payroll without being on the access list.
    accounts_team_slug: Mapped[str] = mapped_column(
        String(64), default="accounts", server_default=text("'accounts'"), nullable=False
    )
    #: Tell approvers in the app when a month is submitted, and the submitter
    #: when it is decided. Off by default, so a trial run does not land in the
    #: CEO's notifications.
    notify_in_app: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )

    # ── UAE ──────────────────────────────────────────────────────────
    ae_company_name: Mapped[str] = mapped_column(
        String(200), default="Hamdaz", server_default=text("'Hamdaz'"), nullable=False
    )
    ae_company_address: Mapped[str | None] = mapped_column(Text)
    #: ``fixed_30``: a day's pay is a month's pay / 30. ``calendar``: / the
    #: days in that month.
    ae_day_basis: Mapped[str] = mapped_column(
        String(12), default="fixed_30", server_default=text("'fixed_30'"), nullable=False
    )
    ae_hours_per_day: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), default=Decimal("8"), server_default=text("8"), nullable=False
    )
    #: Ordinary overtime: basic hourly × this. Labour law minimum is 1.25.
    ae_ot_normal_rate: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), default=Decimal("1.25"), server_default=text("1.25"), nullable=False
    )
    #: Night (10 pm – 4 am), rest-day and holiday overtime. Law minimum 1.5.
    ae_ot_premium_rate: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), default=Decimal("1.50"), server_default=text("1.50"), nullable=False
    )
    #: The establishment's 13-digit MOHRE (labour) id — the SIF file's employer id.
    ae_employer_mol_id: Mapped[str | None] = mapped_column(String(20))
    #: The paying bank's 9-digit routing code.
    ae_employer_bank_routing: Mapped[str | None] = mapped_column(String(20))
    #: Free text the bank shows against the file, if it asks for one.
    ae_employer_reference: Mapped[str | None] = mapped_column(String(60))

    # ── India ────────────────────────────────────────────────────────
    in_company_name: Mapped[str] = mapped_column(
        String(200), default="Hamdaz", server_default=text("'Hamdaz'"), nullable=False
    )
    in_company_address: Mapped[str | None] = mapped_column(Text)
    in_day_basis: Mapped[str] = mapped_column(
        String(12), default="calendar", server_default=text("'calendar'"), nullable=False
    )
    in_pf_establishment: Mapped[str | None] = mapped_column(String(40))
    in_pf_ee_rate: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), default=Decimal("12"), server_default=text("12"), nullable=False
    )
    in_pf_er_rate: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), default=Decimal("12"), server_default=text("12"), nullable=False
    )
    #: The part of the employer's share that goes to the pension scheme.
    in_eps_rate: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), default=Decimal("8.33"), server_default=text("8.33"), nullable=False
    )
    #: PF wage ceiling. Contributions above it are voluntary.
    in_pf_ceiling: Mapped[Decimal] = mapped_column(
        Money, default=Decimal("15000"), server_default=text("15000"), nullable=False
    )
    in_edli_rate: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), default=Decimal("0.50"), server_default=text("0.50"), nullable=False
    )
    in_pf_admin_rate: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), default=Decimal("0.50"), server_default=text("0.50"), nullable=False
    )
    #: The labour codes' 50 % rule: when allowances are more than half of pay,
    #: the excess counts as wages for PF. Off until the PF consultant says so.
    in_wage_code_rule: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    in_esi_threshold: Mapped[Decimal] = mapped_column(
        Money, default=Decimal("21000"), server_default=text("21000"), nullable=False
    )
    in_esi_ee_rate: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), default=Decimal("0.75"), server_default=text("0.75"), nullable=False
    )
    in_esi_er_rate: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), default=Decimal("3.25"), server_default=text("3.25"), nullable=False
    )
    #: Professional tax by state: ``{"KA": [{"min_gross": "25000", "amount":
    #: "200", "feb_amount": null}], ...}``. Monthly gross picks the slab.
    in_pt_slabs: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    #: Income tax tables (new regime) by financial year's starting year:
    #: ``{"2025": {...}}``. See ``app/payroll/rules.TaxTable``.
    in_tax_tables: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )

    # ── invoicing ────────────────────────────────────────────────────
    #: Our own invoice numbers: ``HZ-INV-2026-001``. Zoho gives its own as well.
    invoice_prefix: Mapped[str] = mapped_column(
        String(20), default="HZ-INV-", server_default=text("'HZ-INV-'"), nullable=False
    )
    #: Days to pay, the default on a new invoice.
    invoice_payment_terms: Mapped[int] = mapped_column(
        Integer, default=30, server_default=text("30"), nullable=False
    )
    #: Customer notes and terms printed on every new invoice, editable on each.
    invoice_notes: Mapped[str | None] = mapped_column(Text)
    invoice_terms: Mapped[str | None] = mapped_column(Text)
    #: The Zoho contact a new invoice starts with — usually the one company
    #: payroll is invoiced to. The accountant can change it on any invoice.
    invoice_customer_zoho_id: Mapped[str | None] = mapped_column(String(40))
    invoice_customer_name: Mapped[str | None] = mapped_column(String(200))
    #: Mail the super admins when an invoice is sent for approval, and the
    #: report (with the PDF) to the super admins, the CEO and Accounts once it
    #: is in Zoho. Off by default: a trial invoice must not reach the CEO.
    invoice_mail: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )

    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


class PayrollMember(Base, UUIDPrimaryKey, Timestamped):
    """Somebody a super admin has let into payroll by name."""

    __tablename__ = "payroll_members"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    level: Mapped[AccessLevel] = mapped_column(String(12), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    added_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    user: Mapped[User] = relationship(foreign_keys=[user_id], lazy="joined")


class PayrollEmployee(Base, UUIDPrimaryKey, Timestamped):
    """A person on payroll.

    Not the same thing as a user. Site staff are paid without ever signing in
    here, so the link to a login is optional — and it is what lets approved
    unpaid leave flow into the month without anyone typing it twice.
    """

    __tablename__ = "payroll_employees"

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), unique=True
    )
    code: Mapped[str] = mapped_column(String(40), unique=True, index=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320))
    designation: Mapped[str | None] = mapped_column(String(120))
    department: Mapped[str | None] = mapped_column(String(120))
    country: Mapped[Country] = mapped_column(String(2), nullable=False, index=True)
    joined_on: Mapped[date] = mapped_column(Date, nullable=False)
    #: Last working day. Set, and the person drops out of months after it.
    left_on: Mapped[date | None] = mapped_column(Date)

    bank_name: Mapped[str | None] = mapped_column(String(120))
    bank_account: Mapped[str | None] = mapped_column(String(40))
    #: UAE.
    iban: Mapped[str | None] = mapped_column(String(34))
    #: UAE: the employee's bank or exchange house routing code (WPS agent id).
    bank_routing: Mapped[str | None] = mapped_column(String(20))
    #: UAE: the 14-digit MOHRE person id on the labour card.
    mol_person_id: Mapped[str | None] = mapped_column(String(20))
    #: India.
    ifsc: Mapped[str | None] = mapped_column(String(11))
    pan: Mapped[str | None] = mapped_column(String(10))
    uan: Mapped[str | None] = mapped_column(String(12))
    esi_number: Mapped[str | None] = mapped_column(String(17))
    #: Two-letter state code for professional tax: KA, MH, KL...
    pt_state: Mapped[str | None] = mapped_column(String(4))
    pf_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    #: Contribute PF on actual basic rather than the ceiling.
    pf_on_actual: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    #: Member of the pension scheme. False for people who joined above the
    #: ceiling after 2014, or are over 58: their employer share all goes to PF.
    eps_member: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    #: ``auto`` (by the wage threshold), ``yes`` or ``no``.
    esi_mode: Mapped[str] = mapped_column(
        String(4), default="auto", server_default=text("'auto'"), nullable=False
    )
    #: A fixed monthly professional tax, instead of the state's slabs.
    pt_override: Mapped[Decimal | None] = mapped_column(Money)
    #: A fixed monthly TDS, for anyone on the old regime or a special case.
    tds_monthly_override: Mapped[Decimal | None] = mapped_column(Money)
    #: Salary and TDS for months of the financial year paid outside this
    #: system. Without it, the first month here projects tax from zero.
    opening_fy: Mapped[int | None] = mapped_column(Integer)
    opening_taxable: Mapped[Decimal] = mapped_column(
        Money, default=Decimal("0"), server_default=text("0"), nullable=False
    )
    opening_tds: Mapped[Decimal] = mapped_column(
        Money, default=Decimal("0"), server_default=text("0"), nullable=False
    )

    notes: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    salaries: Mapped[list[PayrollSalary]] = relationship(
        back_populates="employee",
        cascade="all, delete-orphan",
        order_by="PayrollSalary.effective_from",
        lazy="selectin",
    )

    @property
    def currency(self) -> str:
        return CURRENCY[self.country]


class PayrollSalary(Base, UUIDPrimaryKey, Timestamped):
    """One salary structure, from a date. A revision is a new row, not an edit.

    ``components`` is a list of ``{"code", "name", "amount"}`` — monthly
    amounts, before any proration. The codes come from
    ``app/payroll/rules.COMPONENTS``; ``OTHER`` may repeat under different names.
    """

    __tablename__ = "payroll_salaries"
    __table_args__ = (
        UniqueConstraint("employee_id", "effective_from", name="uq_payroll_salary_from"),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("payroll_employees.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    components: Mapped[list] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    note: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    employee: Mapped[PayrollEmployee] = relationship(back_populates="salaries")


class PayrollRun(Base, UUIDPrimaryKey, Timestamped):
    """One country's payroll for one month."""

    __tablename__ = "payroll_runs"
    __table_args__ = (
        UniqueConstraint("country", "year", "month", name="uq_payroll_run_period"),
    )

    country: Mapped[Country] = mapped_column(String(2), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    month: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[RunStatus] = mapped_column(
        String(12), default=RunStatus.DRAFT, nullable=False, index=True
    )

    headcount: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    gross: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    deductions: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    net: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    employer_cost: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    #: Things the processor should look at before submitting. Recomputed with
    #: the run; an empty list is the goal, not a requirement.
    warnings: Mapped[list] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    note: Mapped[str | None] = mapped_column(Text)
    calculated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    submitted_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: The approver's note on the last return, kept until the next submission.
    returned_note: Mapped[str | None] = mapped_column(Text)
    paid_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    paid_on: Mapped[date | None] = mapped_column(Date)

    created_by: Mapped[User | None] = relationship(foreign_keys=[created_by_id], lazy="joined")
    submitted_by: Mapped[User | None] = relationship(
        foreign_keys=[submitted_by_id], lazy="joined"
    )
    approved_by: Mapped[User | None] = relationship(foreign_keys=[approved_by_id], lazy="joined")
    paid_by: Mapped[User | None] = relationship(foreign_keys=[paid_by_id], lazy="joined")

    lines: Mapped[list[PayrollLine]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="PayrollLine.employee_code",
        lazy="selectin",
    )

    @property
    def is_locked(self) -> bool:
        return self.status != RunStatus.DRAFT


class PayrollLine(Base, UUIDPrimaryKey, Timestamped):
    """One employee's month: the inputs, and the payslip calculated from them."""

    __tablename__ = "payroll_lines"
    __table_args__ = (
        UniqueConstraint("run_id", "employee_id", name="uq_payroll_line_employee"),
        Index("ix_payroll_lines_employee", "employee_id"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payroll_runs.id", ondelete="CASCADE"), nullable=False
    )
    #: RESTRICT: an employee who has been paid cannot be deleted out from under
    #: the record of it. The service deletes draft lines itself first.
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("payroll_employees.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # ── snapshot ─────────────────────────────────────────────────────
    employee_code: Mapped[str] = mapped_column(String(40), nullable=False)
    employee_name: Mapped[str] = mapped_column(String(200), nullable=False)
    designation: Mapped[str | None] = mapped_column(String(120))
    #: Bank and statutory identifiers as they were when last calculated.
    identifiers: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )

    # ── inputs the processor sets ────────────────────────────────────
    #: Extra unpaid days on top of approved unpaid leave; negative to cancel some.
    lop_adjust: Mapped[Decimal] = mapped_column(
        Numeric(6, 2), default=Decimal("0"), server_default=text("0"), nullable=False
    )
    ot_normal_hours: Mapped[Decimal] = mapped_column(
        Numeric(7, 2), default=Decimal("0"), server_default=text("0"), nullable=False
    )
    ot_premium_hours: Mapped[Decimal] = mapped_column(
        Numeric(7, 2), default=Decimal("0"), server_default=text("0"), nullable=False
    )
    #: One-off lines: ``{"name", "amount", "kind": "earning"|"deduction",
    #: "variable": bool, "taxable": bool}``.
    extras: Mapped[list] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    tds_override: Mapped[Decimal | None] = mapped_column(Money)
    #: Paid later: stays in the register, left out of the bank file.
    on_hold: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    note: Mapped[str | None] = mapped_column(Text)

    # ── calculated ───────────────────────────────────────────────────
    leave_lop_days: Mapped[Decimal] = mapped_column(
        Numeric(6, 2), default=Decimal("0"), server_default=text("0"), nullable=False
    )
    basis_days: Mapped[Decimal] = mapped_column(
        Numeric(6, 2), default=Decimal("30"), server_default=text("30"), nullable=False
    )
    paid_days: Mapped[Decimal] = mapped_column(
        Numeric(6, 2), default=Decimal("0"), server_default=text("0"), nullable=False
    )
    unpaid_days: Mapped[Decimal] = mapped_column(
        Numeric(6, 2), default=Decimal("0"), server_default=text("0"), nullable=False
    )
    earnings: Mapped[list] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    deductions: Mapped[list] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    employer: Mapped[list] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    gross: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    total_deductions: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    net: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    employer_cost: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    #: India: this month's taxable salary and TDS. Columns, not JSON, because
    #: the next month's projection sums them.
    taxable: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    tds: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    #: How the figures were reached: PF wages, the tax projection, gratuity.
    workings: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    warnings: Mapped[list] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )

    run: Mapped[PayrollRun] = relationship(back_populates="lines")


class PayrollEvent(Base, UUIDPrimaryKey, Timestamped):
    """Every change to payroll, and who made it.

    Kept apart from the rows it describes so that deleting a draft run or an
    employee does not delete the record that it existed — the ids go NULL and
    ``summary`` still says what it was.
    """

    __tablename__ = "payroll_events"
    __table_args__ = (Index("ix_payroll_events_created", "created_at"),)

    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payroll_runs.id", ondelete="SET NULL"), index=True
    )
    employee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payroll_employees.id", ondelete="SET NULL"), index=True
    )
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payroll_invoices.id", ondelete="SET NULL"), index=True
    )
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )

    actor: Mapped[User | None] = relationship(foreign_keys=[actor_id], lazy="joined")


class PayrollInvoice(Base, UUIDPrimaryKey, Timestamped):
    """An invoice raised from payroll — the month's salaries, rent, anything else.

    Prepared here, approved here, then created in Zoho Books. Its fields are
    Zoho's invoice fields, so what is approved is exactly what Zoho receives:
    ``zoho_payload`` keeps the body that was (or, in test mode, would have
    been) sent.

    Lines are JSON rather than a table, the same as a payroll line's extras:
    ``{"item_id", "name", "description", "quantity", "rate", "tax_id",
    "tax_name", "tax_percentage", "amount", "tax_amount", "run_id"}``, with
    ``amount`` and ``tax_amount`` worked out by ``app/payroll/invoicing.py``.
    """

    __tablename__ = "payroll_invoices"

    number: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    status: Mapped[InvoiceStatus] = mapped_column(
        String(12), default=InvoiceStatus.DRAFT, nullable=False, index=True
    )

    # ── what Zoho needs ──────────────────────────────────────────────
    #: The Zoho contact billed. Name kept beside it so the list reads without Zoho.
    customer_zoho_id: Mapped[str | None] = mapped_column(String(40))
    customer_name: Mapped[str] = mapped_column(String(200), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    #: Units of the organisation's base currency per unit of ``currency``,
    #: when they differ. Zoho's own ``exchange_rate``.
    exchange_rate: Mapped[Decimal | None] = mapped_column(Numeric(16, 6))
    invoice_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date | None] = mapped_column(Date)
    #: Days to pay; Zoho works the due date out from it when there is none.
    payment_terms: Mapped[int | None] = mapped_column(Integer)
    reference_number: Mapped[str | None] = mapped_column(String(100))
    lines: Mapped[list] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    adjustment: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    adjustment_description: Mapped[str | None] = mapped_column(String(100))
    #: Printed on the invoice: a note to the customer, and the terms.
    notes: Mapped[str | None] = mapped_column(Text)
    terms: Mapped[str | None] = mapped_column(Text)

    sub_total: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    tax_total: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    total: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)

    #: The payroll month this bills, when a line came from one.
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payroll_runs.id", ondelete="SET NULL"), index=True
    )

    # ── the steps ────────────────────────────────────────────────────
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    submitted_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    returned_note: Mapped[str | None] = mapped_column(Text)

    #: ``test`` — checked against Zoho with reads, nothing written; ``live`` —
    #: created in Zoho. Empty until "Create in Zoho" has been pressed.
    zoho_mode: Mapped[str | None] = mapped_column(String(8))
    zoho_payload: Mapped[dict | None] = mapped_column(JSONB)
    zoho_invoice_id: Mapped[str | None] = mapped_column(String(40))
    zoho_invoice_number: Mapped[str | None] = mapped_column(String(60))
    zoho_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    zoho_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    #: What the reads found wrong, or what Zoho refused. Cleared on success.
    zoho_error: Mapped[str | None] = mapped_column(Text)
    #: Zoho's own PDF of a live invoice — what the report attaches.
    zoho_pdf: Mapped[bytes | None] = mapped_column(LargeBinary)

    report_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Who the report went to — or, with mail off, would have gone to.
    report_to: Mapped[list] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    report_error: Mapped[str | None] = mapped_column(Text)

    created_by: Mapped[User | None] = relationship(foreign_keys=[created_by_id], lazy="joined")
    submitted_by: Mapped[User | None] = relationship(foreign_keys=[submitted_by_id], lazy="joined")
    approved_by: Mapped[User | None] = relationship(foreign_keys=[approved_by_id], lazy="joined")
    zoho_by: Mapped[User | None] = relationship(foreign_keys=[zoho_by_id], lazy="joined")
