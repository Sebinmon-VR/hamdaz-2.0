"""cost lines belong to one line, and can be per unit

A landed-cost row can now be charged to one priced line instead of the whole
bid — two lines from two suppliers carry two suppliers' charges — and can be
stated per unit, so a charge of 400 each on a line of two costs 800.

Revision ID: d8b3e6f1a492
Revises: c3a7d5e9b120
Create Date: 2026-10-07 12:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'd8b3e6f1a492'
down_revision: str | None = 'c3a7d5e9b120'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('quote_cost_lines', sa.Column('line_position', sa.Integer(), nullable=True))
    op.add_column(
        'quote_cost_lines',
        sa.Column('per_unit', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    )


def downgrade() -> None:
    op.drop_column('quote_cost_lines', 'per_unit')
    op.drop_column('quote_cost_lines', 'line_position')
