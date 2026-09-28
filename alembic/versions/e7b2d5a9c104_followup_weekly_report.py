"""followup weekly report

The end-of-day report's week: the same tasks and reasons over seven days, sent
on one weekday at the closing time, to the same people. On by default, Friday.

Revision ID: e7b2d5a9c104
Revises: d9a4c6e1b852
Create Date: 2026-09-29 16:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'e7b2d5a9c104'
down_revision: str | None = 'd9a4c6e1b852'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('followup_settings', sa.Column('weekly_enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False))
    op.add_column('followup_settings', sa.Column('weekly_day', sa.Integer(), server_default=sa.text('4'), nullable=False))
    op.add_column('followup_settings', sa.Column('weekly_last_sent_on', sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column('followup_settings', 'weekly_last_sent_on')
    op.drop_column('followup_settings', 'weekly_day')
    op.drop_column('followup_settings', 'weekly_enabled')
