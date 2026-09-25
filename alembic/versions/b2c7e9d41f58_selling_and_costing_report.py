"""selling and costing report

The approval request is now a selling & costing report — what the goods cost
landed, what they sell for, what that leaves, and how far a discount can go
before the margin is gone. Nearly all of it is derived on read from what the
quote already stores. These columns are the few facts it states that nothing
else on the quote held: who the goods come from and how they travel, who they
finally go to, and where the business draws its lines in a negotiation.

Cost rows gain a rate: insurance at 1% of the goods, bank charges at 3%. A
rated row is worked out on read rather than typed, so it follows the goods
when the supplier changes.

Revision ID: b2c7e9d41f58
Revises: f4c8a2d91e37
Create Date: 2026-09-24 12:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'b2c7e9d41f58'
down_revision: str | None = 'f4c8a2d91e37'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for name, length in (
        ('supplier_name', 200),
        ('supplier_basis', 120),
        ('supplier_route', 200),
        ('end_user_name', 200),
    ):
        op.add_column('quote_requests', sa.Column(name, sa.String(length=length), nullable=True))
    op.add_column(
        'quote_requests',
        sa.Column('walk_away_margin_percent', sa.Numeric(precision=6, scale=3), nullable=True),
    )
    op.add_column(
        'quote_requests',
        sa.Column('comfortable_margin_percent', sa.Numeric(precision=6, scale=3), nullable=True),
    )
    op.add_column('quote_requests', sa.Column('recommendation', sa.Text(), nullable=True))
    op.add_column('quote_requests', sa.Column('report_notes', sa.Text(), nullable=True))

    op.add_column(
        'quote_cost_lines',
        sa.Column('percent', sa.Numeric(precision=7, scale=3), nullable=True),
    )
    op.add_column('quote_cost_lines', sa.Column('percent_of', sa.String(length=12), nullable=True))


def downgrade() -> None:
    op.drop_column('quote_cost_lines', 'percent_of')
    op.drop_column('quote_cost_lines', 'percent')
    op.drop_column('quote_requests', 'report_notes')
    op.drop_column('quote_requests', 'recommendation')
    op.drop_column('quote_requests', 'comfortable_margin_percent')
    op.drop_column('quote_requests', 'walk_away_margin_percent')
    op.drop_column('quote_requests', 'end_user_name')
    op.drop_column('quote_requests', 'supplier_route')
    op.drop_column('quote_requests', 'supplier_basis')
    op.drop_column('quote_requests', 'supplier_name')
