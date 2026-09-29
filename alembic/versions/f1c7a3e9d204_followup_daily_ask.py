"""followup daily ask

Ask once a day at a set time instead of a grace after each due time: the mode,
the time and the day it last ran on the settings; and on each follow-up whether
it was carried over from the previous day's batch. And a testing address: while
set, every follow-up email goes there instead — seeded to Sebin for the trial.

Revision ID: f1c7a3e9d204
Revises: e8a4c2f7b951
Create Date: 2026-09-30 11:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'f1c7a3e9d204'
down_revision: str | None = 'e8a4c2f7b951'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('followup_settings', sa.Column('ask_mode', sa.String(length=16), server_default=sa.text("'after_due'"), nullable=False))
    op.add_column('followup_settings', sa.Column('ask_time', sa.String(length=5), server_default=sa.text("'16:00'"), nullable=False))
    op.add_column('followup_settings', sa.Column('ask_last_run_on', sa.Date(), nullable=True))
    op.add_column('followup_settings', sa.Column('test_mail_to', sa.String(length=320), nullable=True))
    op.execute("UPDATE followup_settings SET test_mail_to = 'sebin@hamdaz.com'")
    op.add_column('task_followups', sa.Column('carried_over', sa.Boolean(), server_default=sa.text('false'), nullable=False))


def downgrade() -> None:
    op.drop_column('task_followups', 'carried_over')
    op.drop_column('followup_settings', 'test_mail_to')
    op.drop_column('followup_settings', 'ask_last_run_on')
    op.drop_column('followup_settings', 'ask_time')
    op.drop_column('followup_settings', 'ask_mode')
