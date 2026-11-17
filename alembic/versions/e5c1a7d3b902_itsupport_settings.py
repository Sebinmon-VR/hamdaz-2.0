"""it support settings

One settings row for IT Support in Teams, changed on /admin/it-support: the
switch, whose chats it answers, the check-ins, the model, what it may do per
module, and the worker's heartbeat. The row itself is made on first read,
seeded from the IT_SUPPORT_* environment values. See app/itsupport/settings.py.

Revision ID: e5c1a7d3b902
Revises: d2a8f4c6e917
Create Date: 2026-11-17 12:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = 'e5c1a7d3b902'
down_revision: str | None = 'd2a8f4c6e917'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'itsupport_settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('people', postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column('show_timing', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('due_enabled', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('due_from', sa.String(length=5), server_default='09:00', nullable=False),
        sa.Column('due_until', sa.String(length=5), server_default='17:00', nullable=False),
        sa.Column(
            'due_days', postgresql.ARRAY(sa.Integer()),
            server_default=sa.text("'{0,1,2,3,4,5}'::integer[]"), nullable=False,
        ),
        sa.Column('due_test_to', sa.String(length=320), server_default='', nullable=False),
        sa.Column('brain_model', sa.String(length=60), server_default='claude-haiku-5-5', nullable=False),
        sa.Column('brain_effort', sa.String(length=10), server_default='high', nullable=False),
        sa.Column('module_modes', postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column('running_on', sa.String(length=200), nullable=True),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('signed_in_as', sa.String(length=320), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('updated_by_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['updated_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('itsupport_settings')
