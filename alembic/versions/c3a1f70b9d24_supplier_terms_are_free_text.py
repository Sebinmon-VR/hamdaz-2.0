"""supplier terms are free text

A quotation's delivery time, validity, payment terms, warranty and per-line
lead time are transcriptions of what a supplier wrote, not codes from a list.
Real ones carry their conditions — "2 weeks, ex stock, subject to export
clearance" — and a length cap either truncates the condition away or rejects
the document outright, which is what it did.

Revision ID: c3a1f70b9d24
Revises: 621f6750ca00
Create Date: 2026-09-04 12:10:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'c3a1f70b9d24'
down_revision: str | None = '621f6750ca00'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: column, and the width it had, so the downgrade can put it back.
_WIDENED = [
    ('supplier_quotes', 'validity', 120),
    ('supplier_quotes', 'delivery_time', 120),
    ('supplier_quotes', 'payment_terms', 200),
    ('supplier_quotes', 'warranty', 200),
    ('supplier_quote_items', 'lead_time', 120),
]


def upgrade() -> None:
    for table, column, _ in _WIDENED:
        op.alter_column(
            table, column, type_=sa.Text(), existing_type=sa.String(), existing_nullable=True
        )


def downgrade() -> None:
    # Lossy by nature: anything longer than the old cap is cut to fit, which is
    # the state this migration exists to get out of.
    for table, column, width in _WIDENED:
        op.execute(
            f"UPDATE {table} SET {column} = left({column}, {width}) "
            f"WHERE length({column}) > {width}"
        )
        op.alter_column(
            table,
            column,
            type_=sa.String(length=width),
            existing_type=sa.Text(),
            existing_nullable=True,
        )
