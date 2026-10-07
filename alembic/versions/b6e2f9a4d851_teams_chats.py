"""ai employee accounts and teams chats

An AI employee's own Microsoft 365 account (its Teams chats and mailbox,
through Graph as that account), and the Teams chats it answers. Two columns on
ai_employees: the account's address, and whether it answers in Teams. See
app/msteams.

Revision ID: b6e2f9a4d851
Revises: a4d8e1f7c203
Create Date: 2026-10-06 20:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'b6e2f9a4d851'
down_revision: str | None = 'a4d8e1f7c203'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _stamps() -> list[sa.Column]:
    return [
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    ]


def upgrade() -> None:
    op.add_column('ai_employees', sa.Column('ms_account_email', sa.String(length=320), nullable=True))
    op.add_column(
        'ai_employees',
        sa.Column('teams_enabled', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    )
    op.create_table(
        'ai_employee_accounts',
        sa.Column('employee_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('email', sa.String(length=320), nullable=False),
        sa.Column('entra_object_id', sa.String(length=64), nullable=False),
        sa.Column('display_name', sa.String(length=200), nullable=True),
        sa.Column('refresh_token_enc', sa.Text(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('connected_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('connected_by_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('watermarks', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('last_poll_at', sa.DateTime(timezone=True), nullable=True),
        *_stamps(),
        sa.ForeignKeyConstraint(['employee_id'], ['ai_employees.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['connected_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('employee_id'),
    )
    op.create_table(
        'teams_chats',
        sa.Column('id', postgresql.UUID(as_uuid=True), server_default=sa.func.gen_random_uuid(), nullable=False),
        sa.Column('employee_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('teams_chat_id', sa.String(length=400), nullable=False),
        sa.Column('chat_type', sa.String(length=20), nullable=True),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('assistant_conversation_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('pending_run_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('last_message_at', sa.DateTime(timezone=True), nullable=True),
        *_stamps(),
        sa.ForeignKeyConstraint(['employee_id'], ['ai_employees.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['assistant_conversation_id'], ['assistant_conversations.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('employee_id', 'teams_chat_id', 'user_id'),
    )


def downgrade() -> None:
    op.drop_table('teams_chats')
    op.drop_table('ai_employee_accounts')
    op.drop_column('ai_employees', 'teams_enabled')
    op.drop_column('ai_employees', 'ms_account_email')
