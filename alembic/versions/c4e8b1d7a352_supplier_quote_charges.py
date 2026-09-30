"""supplier quote charges

Duty, handling, insurance, clearance and the like, as a supplier stated them —
from the notes as much as the price table — kept on the supplier quote so the
costing can use them when that supplier is chosen.

Revision ID: c4e8b1d7a352
Revises: a9d3e6b2c815
Create Date: 2026-09-30 18:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'c4e8b1d7a352'
down_revision: str | None = 'a9d3e6b2c815'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'supplier_quotes',
        sa.Column('charges', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('supplier_quotes', 'charges')
