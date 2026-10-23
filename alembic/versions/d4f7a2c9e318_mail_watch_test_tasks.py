"""mail watch test tasks

New tenders from the CEO's mail, assigned (priority 1 of the team, or the
earlier assignee on a stage change) and kept in a test list with the Proposals
list's own columns instead of being written there; and the switch for it.

Revision ID: d4f7a2c9e318
Revises: c8e4b2f9d615
Create Date: 2026-10-23 12:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'd4f7a2c9e318'
down_revision: str | None = 'c8e4b2f9d615'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'mailwatch_teams',
        sa.Column('assign_new_tenders', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    )
    op.add_column('mailwatch_teams', sa.Column('assign_from', sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        'mailwatch_test_tasks',
        sa.Column('id', postgresql.UUID(as_uuid=True), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('mail_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('action', sa.String(length=20), nullable=False),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('assigned_to_name', sa.String(length=200), nullable=True),
        sa.Column('assigned_to_email', sa.String(length=320), nullable=True),
        sa.Column('assigned_to_lookup_id', sa.String(length=20), nullable=True),
        sa.Column('priority', sa.String(length=20), nullable=True),
        sa.Column('start_date', sa.DateTime(timezone=True), nullable=True),
        sa.Column('due_date', sa.DateTime(timezone=True), nullable=True),
        sa.Column('bcd', sa.DateTime(timezone=True), nullable=True),
        sa.Column('status', sa.String(length=40), nullable=True),
        sa.Column('end_user', sa.String(length=300), nullable=True),
        sa.Column('submission_status', sa.String(length=40), nullable=True),
        sa.Column('quote_no', sa.String(length=100), nullable=True),
        sa.Column('current_type', sa.String(length=20), nullable=True),
        sa.Column('remarks', sa.Text(), nullable=True),
        sa.Column('order_status', sa.String(length=40), nullable=True),
        sa.Column('negotiation', sa.String(length=10), nullable=True),
        sa.Column('negotiation_notes', sa.Text(), nullable=True),
        sa.Column('dev_notes', sa.Text(), nullable=True),
        sa.Column('previous_owner', sa.String(length=200), nullable=True),
        sa.Column('working_notes', sa.Text(), nullable=True),
        sa.Column('followup', sa.String(length=10), nullable=True),
        sa.Column('fields', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('assign_reason', sa.Text(), nullable=False),
        sa.Column('standing', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('previous_item_id', sa.String(length=64), nullable=True),
        sa.Column('previous_title', sa.Text(), nullable=True),
        sa.Column('previous_stage', sa.String(length=20), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['mail_id'], ['watched_mails.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('mail_id', name='uq_mailwatch_test_tasks_mail'),
    )


def downgrade() -> None:
    op.drop_table('mailwatch_test_tasks')
    op.drop_column('mailwatch_teams', 'assign_from')
    op.drop_column('mailwatch_teams', 'assign_new_tenders')
