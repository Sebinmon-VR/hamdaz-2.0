"""payroll invoices

Invoices raised from payroll (the month's salaries, rent, anything else),
approved here and created in Zoho Books; the invoicing defaults on payroll
settings; and an invoice column on the payroll log. See app/payroll/invoicing.py.

Revision ID: f3b7d1a9c5e2
Revises: c4e8a1f7d2b6
Create Date: 2026-11-04 17:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f3b7d1a9c5e2"
down_revision: str | None = "c4e8a1f7d2b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())
MONEY = sa.Numeric(16, 2)


def _user_fk(name: str) -> sa.Column:
    return sa.Column(name, UUID, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)


def upgrade() -> None:
    op.create_table(
        "payroll_invoices",
        sa.Column("id", UUID, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("number", sa.String(40), nullable=False),
        sa.Column("status", sa.String(12), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("customer_zoho_id", sa.String(40), nullable=True),
        sa.Column("customer_name", sa.String(200), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("exchange_rate", sa.Numeric(16, 6), nullable=True),
        sa.Column("invoice_date", sa.Date(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("payment_terms", sa.Integer(), nullable=True),
        sa.Column("reference_number", sa.String(100), nullable=True),
        sa.Column("lines", JSONB, server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("adjustment", MONEY, server_default=sa.text("0"), nullable=False),
        sa.Column("adjustment_description", sa.String(100), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("terms", sa.Text(), nullable=True),
        sa.Column("sub_total", MONEY, server_default=sa.text("0"), nullable=False),
        sa.Column("tax_total", MONEY, server_default=sa.text("0"), nullable=False),
        sa.Column("total", MONEY, server_default=sa.text("0"), nullable=False),
        sa.Column(
            "run_id", UUID, sa.ForeignKey("payroll_runs.id", ondelete="SET NULL"), nullable=True
        ),
        _user_fk("created_by_id"),
        _user_fk("submitted_by_id"),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        _user_fk("approved_by_id"),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("returned_note", sa.Text(), nullable=True),
        sa.Column("zoho_mode", sa.String(8), nullable=True),
        sa.Column("zoho_payload", JSONB, nullable=True),
        sa.Column("zoho_invoice_id", sa.String(40), nullable=True),
        sa.Column("zoho_invoice_number", sa.String(60), nullable=True),
        sa.Column("zoho_at", sa.DateTime(timezone=True), nullable=True),
        _user_fk("zoho_by_id"),
        sa.Column("zoho_error", sa.Text(), nullable=True),
        sa.Column("zoho_pdf", sa.LargeBinary(), nullable=True),
        sa.Column("report_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("report_to", JSONB, server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("report_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("number", name="uq_payroll_invoices_number"),
    )
    op.create_index("ix_payroll_invoices_status", "payroll_invoices", ["status"])
    op.create_index("ix_payroll_invoices_run_id", "payroll_invoices", ["run_id"])

    op.add_column(
        "payroll_events",
        sa.Column(
            "invoice_id",
            UUID,
            sa.ForeignKey("payroll_invoices.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_payroll_events_invoice_id", "payroll_events", ["invoice_id"])

    for column in (
        sa.Column(
            "invoice_prefix", sa.String(20), server_default=sa.text("'HZ-INV-'"), nullable=False
        ),
        sa.Column(
            "invoice_payment_terms", sa.Integer(), server_default=sa.text("30"), nullable=False
        ),
        sa.Column("invoice_notes", sa.Text(), nullable=True),
        sa.Column("invoice_terms", sa.Text(), nullable=True),
        sa.Column("invoice_customer_zoho_id", sa.String(40), nullable=True),
        sa.Column("invoice_customer_name", sa.String(200), nullable=True),
        sa.Column("invoice_mail", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    ):
        op.add_column("payroll_settings", column)


def downgrade() -> None:
    for name in (
        "invoice_mail",
        "invoice_customer_name",
        "invoice_customer_zoho_id",
        "invoice_terms",
        "invoice_notes",
        "invoice_payment_terms",
        "invoice_prefix",
    ):
        op.drop_column("payroll_settings", name)
    op.drop_index("ix_payroll_events_invoice_id", table_name="payroll_events")
    op.drop_column("payroll_events", "invoice_id")
    op.drop_table("payroll_invoices")
