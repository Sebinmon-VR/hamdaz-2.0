"""it support features, activity log, sent ledger; reason tags

The agent's feature switches (one JSONB on its settings row, each off until
turned on from the page), its activity log (every change it made, for whom,
and the 👎 feedback and model spend), the ledger of what it has sent once
(a daily ask, an alert), which outlives a redeploy unlike the old local
file, and a tag on each late-task reason for the daily report's grouping.

Revision ID: c8f2a6d4e1b9
Revises: a3c7e9d1f5b2
Create Date: 2026-11-18 12:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = 'c8f2a6d4e1b9'
down_revision: str | None = 'a3c7e9d1f5b2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('itsupport_settings', sa.Column(
        'features', postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False))
    op.create_table(
        'itsupport_activity',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
        sa.Column('email', sa.String(length=320), nullable=False),
        sa.Column('kind', sa.String(length=30), nullable=False),
        sa.Column('summary', sa.Text(), nullable=False),
        sa.Column('detail', postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"),
                  nullable=False),
        sa.Column('ok', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('cost_usd', sa.Numeric(10, 5), nullable=True),
    )
    op.create_index('ix_itsupport_activity_at', 'itsupport_activity', ['at'])
    op.create_index('ix_itsupport_activity_kind_at', 'itsupport_activity', ['kind', 'at'])
    op.create_table(
        'itsupport_sent',
        sa.Column('key', sa.String(length=300), primary_key=True),
        sa.Column('email', sa.String(length=320), nullable=False),
        sa.Column('kind', sa.String(length=40), nullable=False),
        sa.Column('sent_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
        sa.Column('detail', postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"),
                  nullable=False),
    )
    op.create_index('ix_itsupport_sent_at', 'itsupport_sent', ['sent_at'])
    op.add_column('task_followups', sa.Column('reason_tag', sa.String(length=40), nullable=True))


def downgrade() -> None:
    op.drop_column('task_followups', 'reason_tag')
    op.drop_index('ix_itsupport_sent_at', table_name='itsupport_sent')
    op.drop_table('itsupport_sent')
    op.drop_index('ix_itsupport_activity_kind_at', table_name='itsupport_activity')
    op.drop_index('ix_itsupport_activity_at', table_name='itsupport_activity')
    op.drop_table('itsupport_activity')
    op.drop_column('itsupport_settings', 'features')
