"""ai employees

Named AI workers built on the assistant, each with a job, rules, a model,
allowed modules, a write mode, an audience and a monthly budget. A chat with
one is an assistant conversation marked with it. See app/assistant/employees.py.

Revision ID: a4d8e1f7c203
Revises: f3c9a7e2b614
Create Date: 2026-10-06 18:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'a4d8e1f7c203'
down_revision: str | None = 'f3c9a7e2b614'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'ai_employees',
        sa.Column('id', postgresql.UUID(as_uuid=True), server_default=sa.func.gen_random_uuid(), nullable=False),
        sa.Column('name', sa.String(length=80), nullable=False),
        sa.Column('title', sa.String(length=120), nullable=False),
        sa.Column('description', sa.Text(), server_default='', nullable=False),
        sa.Column('instructions', sa.Text(), server_default='', nullable=False),
        sa.Column('greeting', sa.Text(), nullable=True),
        sa.Column('color', sa.String(length=16), server_default='#0e5e80', nullable=False),
        sa.Column('model_key', sa.String(length=64), nullable=True),
        sa.Column('reasoning_effort', sa.String(length=16), nullable=True),
        sa.Column(
            'allowed_modules', postgresql.ARRAY(sa.String(length=40)),
            server_default=sa.text("'{}'::varchar[]"), nullable=False,
        ),
        sa.Column('write_mode', sa.String(length=16), server_default='read_only', nullable=False),
        sa.Column(
            'audience_roles', postgresql.ARRAY(sa.String(length=40)),
            server_default=sa.text("'{}'::varchar[]"), nullable=False,
        ),
        sa.Column('monthly_budget_usd', sa.Numeric(10, 2), nullable=True),
        sa.Column('enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('sort_order', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('created_by_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('updated_by_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['updated_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('ai_employees')
