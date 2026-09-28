"""task followups

Asking the holder of a task that went past its due date why, and keeping what
they answered. Two tables: the settings (one row) and one row per missed
deadline. See app/models/followup.py.

Revision ID: c5e2a9d4f713
Revises: b3d7e1f0c9a2
Create Date: 2026-09-28 13:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'c5e2a9d4f713'
down_revision: str | None = 'b3d7e1f0c9a2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'followup_settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('enabled', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('team_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            'only_emails', postgresql.ARRAY(sa.String(length=320)),
            server_default=sa.text("'{}'::varchar[]"), nullable=False,
        ),
        sa.Column('only_title_contains', sa.String(length=120), server_default=sa.text("''"), nullable=False),
        sa.Column('grace_minutes', sa.Integer(), server_default=sa.text('20'), nullable=False),
        sa.Column('poll_seconds', sa.Integer(), server_default=sa.text('120'), nullable=False),
        sa.Column('watch_from', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ask_from_user_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('notify_managers_by_email', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('last_run_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('updated_by_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['ask_from_user_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['updated_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'task_followups',
        sa.Column('id', postgresql.UUID(as_uuid=True), server_default=sa.func.gen_random_uuid(), nullable=False),
        sa.Column('task_id', sa.String(length=64), nullable=False),
        sa.Column('task_title', sa.Text(), nullable=False),
        sa.Column('task_url', sa.Text(), nullable=True),
        sa.Column('end_user', sa.String(length=300), nullable=True),
        sa.Column('status_at_ask', sa.String(length=80), nullable=True),
        sa.Column('due_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('task_modified_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('team_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('assignee_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('assignee_email', sa.String(length=320), nullable=False),
        sa.Column('asked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ask_error', sa.Text(), nullable=True),
        sa.Column('asked_from_email', sa.String(length=320), nullable=True),
        sa.Column('status', sa.String(length=20), server_default=sa.text("'pending'"), nullable=False),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('answered_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('forwarded_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('forward_error', sa.Text(), nullable=True),
        sa.Column('resolved_note', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['assignee_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('task_id', 'due_at', name='uq_task_followup_deadline'),
    )
    op.create_index('ix_task_followups_task_id', 'task_followups', ['task_id'])
    op.create_index('ix_task_followups_team_id', 'task_followups', ['team_id'])
    op.create_index('ix_task_followups_assignee', 'task_followups', ['assignee_id', 'created_at'])
    op.create_index('ix_task_followups_team', 'task_followups', ['team_id', 'created_at'])
    op.create_index('ix_task_followups_status', 'task_followups', ['status'])

    # On from the start, for presales, so nobody has to switch it on. Trial
    # scope first: Sebin's own tasks with "test" in the title. A super admin
    # clears those two filters on the Overdue tasks screen to cover the whole
    # team. The watch starts now — nothing already overdue is asked about.
    op.execute(
        """
        INSERT INTO followup_settings
            (id, enabled, team_id, only_emails, only_title_contains, watch_from)
        SELECT 1, true, t.id, ARRAY['sebin@hamdaz.com']::varchar[], 'test', now()
        FROM teams t WHERE t.slug = 'presales'
        ON CONFLICT (id) DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_index('ix_task_followups_status', table_name='task_followups')
    op.drop_index('ix_task_followups_team', table_name='task_followups')
    op.drop_index('ix_task_followups_assignee', table_name='task_followups')
    op.drop_index('ix_task_followups_team_id', table_name='task_followups')
    op.drop_index('ix_task_followups_task_id', table_name='task_followups')
    op.drop_table('task_followups')
    op.drop_table('followup_settings')
