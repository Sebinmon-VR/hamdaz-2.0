"""mail watch order cards

A customer's purchase order is its own category now, and gets a Teams card:
the switch for posting them, and the moment it was switched on (no backlog).

Revision ID: b7e3d1f5a924
Revises: a6d2f8c4b931
Create Date: 2026-10-29 12:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'b7e3d1f5a924'
down_revision: str | None = 'a6d2f8c4b931'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'mailwatch_teams',
        sa.Column('post_orders', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    )
    op.add_column('mailwatch_teams', sa.Column('orders_from', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('mailwatch_teams', 'orders_from')
    op.drop_column('mailwatch_teams', 'post_orders')
