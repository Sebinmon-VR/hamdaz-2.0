"""quote approval settings

Who is emailed when a quote is sent for approval, set by a super admin on the
quote requests page: the team's approvers and managers, the global managers,
the CEO, the super admins, and any other addresses. The defaults are what the
code did until now: approvers and managers yes, CEO and super admins no.

Revision ID: e3b6c8d1f947
Revises: d7f2a9c4e618
Create Date: 2026-09-30 20:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'e3b6c8d1f947'
down_revision: str | None = 'd7f2a9c4e618'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'quote_approval_settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('notify_team_approvers', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('notify_team_managers', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('notify_managers', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('notify_ceo', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('notify_super_admins', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column(
            'extra_emails',
            postgresql.ARRAY(sa.String(length=320)),
            server_default=sa.text("'{}'::varchar[]"),
            nullable=False,
        ),
        sa.Column('updated_by_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['updated_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('quote_approval_settings')
