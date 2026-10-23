"""mail watch teams

Negotiation updates from the CEO's mail, posted to a Teams channel as a
connected account: one row for that connection (its refresh token encrypted,
the locked channel, the switch — off), and on each mail when it was posted.

Revision ID: c8e4b2f9d615
Revises: f7c2d8e1a493
Create Date: 2026-10-22 21:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'c8e4b2f9d615'
down_revision: str | None = 'f7c2d8e1a493'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'mailwatch_teams',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('post_negotiations', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('posting_from', sa.DateTime(timezone=True), nullable=True),
        sa.Column('refresh_token_enc', sa.Text(), nullable=True),
        sa.Column('account_email', sa.String(length=320), nullable=True),
        sa.Column('account_name', sa.String(length=200), nullable=True),
        sa.Column('account_id', sa.String(length=64), nullable=True),
        sa.Column('connected_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('connected_by_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('team_id', sa.String(length=64), nullable=True),
        sa.Column('team_name', sa.String(length=200), nullable=True),
        sa.Column('channel_id', sa.String(length=200), nullable=True),
        sa.Column('channel_name', sa.String(length=200), nullable=True),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['connected_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.add_column('watched_mails', sa.Column('teams_posted_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('watched_mails', sa.Column('teams_message_id', sa.String(length=100), nullable=True))
    op.add_column('watched_mails', sa.Column('teams_error', sa.Text(), nullable=True))
    op.add_column(
        'watched_mails',
        sa.Column('teams_attempts', sa.Integer(), server_default=sa.text('0'), nullable=False),
    )


def downgrade() -> None:
    op.drop_column('watched_mails', 'teams_attempts')
    op.drop_column('watched_mails', 'teams_error')
    op.drop_column('watched_mails', 'teams_message_id')
    op.drop_column('watched_mails', 'teams_posted_at')
    op.drop_table('mailwatch_teams')
