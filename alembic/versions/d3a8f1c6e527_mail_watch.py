"""mail watch

The CEO's mails in a chosen mailbox, read as Graph reports them. One settings
row (off, mailbox a setting rather than an environment value) and one row per
mail from a watched sender. Read only. See app/mailwatch.

Revision ID: d3a8f1c6e527
Revises: b8e4f2a6d915
Create Date: 2026-10-22 12:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'd3a8f1c6e527'
down_revision: str | None = 'b8e4f2a6d915'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'mailwatch_settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('enabled', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('mailbox', sa.String(length=320), server_default='', nullable=False),
        sa.Column(
            'senders', postgresql.ARRAY(sa.String(length=320)),
            server_default=sa.text("'{}'::varchar[]"), nullable=False,
        ),
        sa.Column('use_model', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('watch_from', sa.DateTime(timezone=True), nullable=True),
        sa.Column('delta_link', sa.Text(), nullable=True),
        sa.Column('subscription_id', sa.String(length=100), nullable=True),
        sa.Column('subscription_secret', sa.String(length=100), nullable=True),
        sa.Column('subscription_mailbox', sa.String(length=320), nullable=True),
        sa.Column('subscription_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_notified_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_read_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('updated_by_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['updated_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'watched_mails',
        sa.Column('id', postgresql.UUID(as_uuid=True), server_default=sa.func.gen_random_uuid(), nullable=False),
        sa.Column('mailbox', sa.String(length=320), nullable=False),
        sa.Column('graph_message_id', sa.String(length=300), nullable=False),
        sa.Column('conversation_id', sa.String(length=300), nullable=True),
        sa.Column('internet_message_id', sa.String(length=500), nullable=True),
        sa.Column('received_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('sender_email', sa.String(length=320), nullable=True),
        sa.Column('sender_name', sa.String(length=200), nullable=True),
        sa.Column('subject', sa.Text(), nullable=True),
        sa.Column('body', sa.Text(), nullable=True),
        sa.Column('has_attachments', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column(
            'attachment_names', postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"), nullable=False,
        ),
        sa.Column('web_link', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('mailbox', 'graph_message_id', name='uq_watched_mails_message'),
    )
    op.create_index('ix_watched_mails_received_at', 'watched_mails', ['received_at'])
    op.create_index('ix_watched_mails_conversation_id', 'watched_mails', ['conversation_id'])


def downgrade() -> None:
    op.drop_index('ix_watched_mails_conversation_id', table_name='watched_mails')
    op.drop_index('ix_watched_mails_received_at', table_name='watched_mails')
    op.drop_table('watched_mails')
    op.drop_table('mailwatch_settings')
