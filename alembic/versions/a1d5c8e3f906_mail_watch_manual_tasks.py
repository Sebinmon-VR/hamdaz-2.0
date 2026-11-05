"""mail watch manual tasks

A Teams card for each task added to the Proposals list by hand: a row per
task posted (once), and the switch, its start and its test-channel lock on
mailwatch_teams. See app/mailwatch/manual.py.

Revision ID: a1d5c8e3f906
Revises: f3b7d1a9c5e2
Create Date: 2026-11-05 14:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'a1d5c8e3f906'
down_revision: str | None = 'f3b7d1a9c5e2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'mailwatch_teams',
        sa.Column('post_manual_tasks', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    )
    op.add_column('mailwatch_teams', sa.Column('manual_from', sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        'mailwatch_teams',
        sa.Column('manual_test_only', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    )
    op.create_table(
        'mailwatch_manual_tasks',
        sa.Column('item_id', sa.String(length=64), nullable=False),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('assigned_email', sa.String(length=320), nullable=True),
        sa.Column('attempts', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('posted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('message_id', sa.String(length=200), nullable=True),
        sa.Column('posted_to', sa.String(length=20), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('item_id'),
    )


def downgrade() -> None:
    op.drop_table('mailwatch_manual_tasks')
    op.drop_column('mailwatch_teams', 'manual_test_only')
    op.drop_column('mailwatch_teams', 'manual_from')
    op.drop_column('mailwatch_teams', 'post_manual_tasks')
