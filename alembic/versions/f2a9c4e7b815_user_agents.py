"""each person's own AI agents, and their runs

Agents were kept in the browser while the builder was made; this is where
they live now. ``user_agents`` holds what the owner built; ``agent_runs`` one
row per go at the job, with every step's result and its sources.

Revision ID: f2a9c4e7b815
Revises: e5c1a8d3f704
Create Date: 2026-10-15 16:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'f2a9c4e7b815'
down_revision: str | None = 'e5c1a8d3f704'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'user_agents',
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('owner_id', sa.UUID(), nullable=False),
        sa.Column('name', sa.String(length=80), nullable=False),
        sa.Column('role', sa.String(length=200), server_default=sa.text("''"), nullable=False),
        sa.Column('avatar', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column('instructions', sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column('steps', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_user_agents_owner', 'user_agents', ['owner_id'])

    op.create_table(
        'agent_runs',
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('agent_id', sa.UUID(), nullable=False),
        sa.Column('owner_id', sa.UUID(), nullable=False),
        sa.Column('task_id', sa.String(length=64), nullable=True),
        sa.Column('task_title', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=16), server_default=sa.text("'running'"), nullable=False),
        sa.Column('plan', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column('results', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column('question', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('model', sa.String(length=60), nullable=True),
        sa.Column('input_tokens', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('output_tokens', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('web_searches', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('cost_usd', sa.Numeric(precision=10, scale=4), server_default=sa.text('0'), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['agent_id'], ['user_agents.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_agent_runs_agent', 'agent_runs', ['agent_id', 'created_at'])
    op.create_index('ix_agent_runs_owner', 'agent_runs', ['owner_id'])


def downgrade() -> None:
    op.drop_index('ix_agent_runs_owner', table_name='agent_runs')
    op.drop_index('ix_agent_runs_agent', table_name='agent_runs')
    op.drop_table('agent_runs')
    op.drop_index('ix_user_agents_owner', table_name='user_agents')
    op.drop_table('user_agents')
