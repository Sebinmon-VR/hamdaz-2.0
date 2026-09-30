"""supplier quote details

Who the supplier behind an offer is: address, contacts, registration, bank and
terms, as confirmed on the quote's summary tab, and what the document said
about them, offered as suggestions. Kept per supplier quote, field by field,
so a supplier library can be built from these rows later.

Revision ID: d7f2a9c4e618
Revises: c4e8b1d7a352
Create Date: 2026-09-30 19:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'd7f2a9c4e618'
down_revision: str | None = 'c4e8b1d7a352'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'supplier_quotes',
        sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        'supplier_quotes',
        sa.Column('detail_suggestions', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('supplier_quotes', 'detail_suggestions')
    op.drop_column('supplier_quotes', 'details')
