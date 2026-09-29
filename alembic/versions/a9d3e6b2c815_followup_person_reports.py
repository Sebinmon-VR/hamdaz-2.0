"""followup person reports

In daily mode the managers get one report per person at the closing time — the
reasons given, the tasks already updated and the ones not answered — instead of
a mail when the person answers their last task. The day it last went.

Revision ID: a9d3e6b2c815
Revises: f1c7a3e9d204
Create Date: 2026-09-30 13:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'a9d3e6b2c815'
down_revision: str | None = 'f1c7a3e9d204'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('followup_settings', sa.Column('summaries_last_sent_on', sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column('followup_settings', 'summaries_last_sent_on')
