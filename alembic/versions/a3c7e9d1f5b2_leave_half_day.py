"""leave half day

Leave taken as half a day: ``leave_requests.half_day`` is the morning or the
afternoon of one date (start == end), counted as 0.5. Casual leave needs no
change: the leave type is free text.

Revision ID: a3c7e9d1f5b2
Revises: f1b8d2c6a4e7
Create Date: 2026-11-17 18:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'a3c7e9d1f5b2'
down_revision: str | None = 'f1b8d2c6a4e7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('leave_requests', sa.Column('half_day', sa.String(length=10), nullable=True))
    op.create_check_constraint(
        'ck_leave_half_day_one_date', 'leave_requests',
        "half_day IS NULL OR (half_day IN ('morning', 'afternoon') AND start_date = end_date)")


def downgrade() -> None:
    op.drop_constraint('ck_leave_half_day_one_date', 'leave_requests', type_='check')
    op.drop_column('leave_requests', 'half_day')
