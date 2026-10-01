"""task calendar

Each open task's BCD as an event in its holder's Outlook calendar, with
Outlook's reminder two days ahead. One settings row (off) and one row per
event written. See app/taskcalendar.

Revision ID: d2f7a1c8e590
Revises: c6e1d9a4b273
Create Date: 2026-10-01 21:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'd2f7a1c8e590'
down_revision: str | None = 'c6e1d9a4b273'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'task_calendar_settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('enabled', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('reminder_minutes', sa.Integer(), server_default=sa.text('2880'), nullable=False),
        sa.Column(
            'only_emails', postgresql.ARRAY(sa.String(length=320)),
            server_default=sa.text("'{}'::varchar[]"), nullable=False,
        ),
        sa.Column('only_title_contains', sa.String(length=200), server_default='', nullable=False),
        sa.Column('last_run_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('updated_by_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['updated_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'task_calendar_events',
        sa.Column('id', postgresql.UUID(as_uuid=True), server_default=sa.func.gen_random_uuid(), nullable=False),
        sa.Column('task_id', sa.String(length=64), nullable=False),
        sa.Column('task_title', sa.Text(), nullable=False),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('owner', sa.String(length=320), nullable=False),
        sa.Column('event_id', sa.Text(), nullable=False),
        sa.Column('bcd_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('reminder_minutes', sa.Integer(), nullable=False),
        sa.Column('synced_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('task_id'),
    )


def downgrade() -> None:
    op.drop_table('task_calendar_events')
    op.drop_table('task_calendar_settings')
