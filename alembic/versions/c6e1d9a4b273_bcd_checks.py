"""bcd checks

A new Proposals task's BCD is the moment it was assigned until a person reads
the real closing date from Ariba. These hold such a task in every module and
ask the assignee (team lead copied), then the managers after two working
hours. One settings row (off) and one row per task. See app/bcd.

Revision ID: c6e1d9a4b273
Revises: a8d3f5c2e914
Create Date: 2026-10-01 18:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'c6e1d9a4b273'
down_revision: str | None = 'a8d3f5c2e914'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'bcd_check_settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('enabled', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('work_start', sa.String(length=5), server_default='10:00', nullable=False),
        sa.Column('work_end', sa.String(length=5), server_default='18:00', nullable=False),
        sa.Column('timezone', sa.String(length=64), server_default='Asia/Kolkata', nullable=False),
        sa.Column(
            'work_days', postgresql.ARRAY(sa.Integer()),
            server_default=sa.text("'{0,1,2,3,4,5}'::integer[]"), nullable=False,
        ),
        sa.Column('escalate_after_minutes', sa.Integer(), server_default=sa.text('120'), nullable=False),
        sa.Column('watch_from', sa.DateTime(timezone=True), nullable=True),
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
        'bcd_checks',
        sa.Column('id', postgresql.UUID(as_uuid=True), server_default=sa.func.gen_random_uuid(), nullable=False),
        sa.Column('task_id', sa.String(length=64), nullable=False),
        sa.Column('task_title', sa.Text(), nullable=False),
        sa.Column('task_url', sa.Text(), nullable=True),
        sa.Column('task_created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('placeholder_bcd', sa.DateTime(timezone=True), nullable=True),
        sa.Column('team_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('assignee_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('assignee_email', sa.String(length=320), nullable=False),
        sa.Column('status', sa.String(length=20), server_default='pending', nullable=False),
        sa.Column('asked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ask_error', sa.Text(), nullable=True),
        sa.Column('escalated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('escalate_error', sa.Text(), nullable=True),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('resolved_by_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('resolved_note', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['assignee_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['resolved_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('task_id'),
    )
    op.create_index('ix_bcd_checks_status', 'bcd_checks', ['status'])


def downgrade() -> None:
    op.drop_index('ix_bcd_checks_status', table_name='bcd_checks')
    op.drop_table('bcd_checks')
    op.drop_table('bcd_check_settings')
