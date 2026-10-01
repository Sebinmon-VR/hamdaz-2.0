"""status reminders

Two days before a task is due, its holder is asked where it stands: Status,
Submission Status, Remarks and Working notes, on a form that can write them
to the Proposals list. One settings row (off, and with the write off), and
one row per reminder. See app/reminders.

Revision ID: a8d3f5c2e914
Revises: e3b6c8d1f947
Create Date: 2026-10-01 12:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'a8d3f5c2e914'
down_revision: str | None = 'e3b6c8d1f947'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'status_reminder_settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('enabled', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('ask_time', sa.String(length=5), server_default='10:00', nullable=False),
        sa.Column('days_before', sa.Integer(), server_default=sa.text('2'), nullable=False),
        sa.Column('write_sharepoint', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column(
            'only_emails',
            postgresql.ARRAY(sa.String(length=320)),
            server_default=sa.text("'{}'::varchar[]"),
            nullable=False,
        ),
        sa.Column('only_title_contains', sa.String(length=200), server_default='', nullable=False),
        sa.Column('last_run_on', sa.Date(), nullable=True),
        sa.Column('last_run_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('updated_by_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['updated_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'status_reminders',
        sa.Column('id', postgresql.UUID(as_uuid=True), server_default=sa.func.gen_random_uuid(), nullable=False),
        sa.Column('task_id', sa.String(length=64), nullable=False),
        sa.Column('task_title', sa.Text(), nullable=False),
        sa.Column('task_url', sa.Text(), nullable=True),
        sa.Column('end_user', sa.String(length=300), nullable=True),
        sa.Column('due_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('status_at_ask', sa.String(length=80), nullable=True),
        sa.Column('submission_at_ask', sa.String(length=80), nullable=True),
        sa.Column('remarks_at_ask', sa.Text(), nullable=True),
        sa.Column('working_notes_at_ask', sa.Text(), nullable=True),
        sa.Column('team_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('assignee_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('assignee_email', sa.String(length=320), nullable=False),
        sa.Column('asked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ask_error', sa.Text(), nullable=True),
        sa.Column('asked_from_email', sa.String(length=320), nullable=True),
        sa.Column('status', sa.String(length=20), server_default='pending', nullable=False),
        sa.Column('answered_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'changes',
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column('written_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('write_error', sa.Text(), nullable=True),
        sa.Column('closed_note', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['assignee_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('task_id', 'due_at', name='uq_status_reminder_deadline'),
    )
    op.create_index('ix_status_reminders_task_id', 'status_reminders', ['task_id'])
    op.create_index('ix_status_reminders_assignee', 'status_reminders', ['assignee_id', 'created_at'])
    op.create_index('ix_status_reminders_status', 'status_reminders', ['status'])


def downgrade() -> None:
    op.drop_index('ix_status_reminders_status', table_name='status_reminders')
    op.drop_index('ix_status_reminders_assignee', table_name='status_reminders')
    op.drop_index('ix_status_reminders_task_id', table_name='status_reminders')
    op.drop_table('status_reminders')
    op.drop_table('status_reminder_settings')
