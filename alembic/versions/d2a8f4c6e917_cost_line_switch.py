"""cost line on/off switch

A landed-cost row can be switched off: it stays on the sheet with its rate but
adds nothing to the price. Every existing row stays on.

Revision ID: d2a8f4c6e917
Revises: b7e3d9a4c218
Create Date: 2026-11-12 12:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'd2a8f4c6e917'
down_revision: str | None = 'b7e3d9a4c218'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'quote_cost_lines',
        sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    )


def downgrade() -> None:
    op.drop_column('quote_cost_lines', 'is_active')
