"""always-on agents: Nova, triggers, and runs that survive a restart

``user_agents`` gains ``system_key`` (the agent the app gives presales,
"nova"), ``always_on`` and ``trigger_state``; ``agent_runs`` gains
``started_by`` and ``heartbeat_at``.

Revision ID: a7d3e9f1c204
Revises: f2a9c4e7b815
Create Date: 2026-10-15 19:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'a7d3e9f1c204'
down_revision: str | None = 'f2a9c4e7b815'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('user_agents', sa.Column('system_key', sa.String(length=32), nullable=True))
    op.add_column('user_agents', sa.Column('always_on', sa.Boolean(), server_default=sa.text('true'), nullable=False))
    op.add_column('user_agents', sa.Column(
        'trigger_state', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False))
    op.create_index(
        'uq_user_agents_owner_system', 'user_agents', ['owner_id', 'system_key'], unique=True,
        postgresql_where=sa.text('system_key IS NOT NULL'))
    op.add_column('agent_runs', sa.Column('started_by', sa.String(length=16), server_default=sa.text("'hand'"), nullable=False))
    op.add_column('agent_runs', sa.Column('heartbeat_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('agent_runs', 'heartbeat_at')
    op.drop_column('agent_runs', 'started_by')
    op.drop_index('uq_user_agents_owner_system', table_name='user_agents')
    op.drop_column('user_agents', 'trigger_state')
    op.drop_column('user_agents', 'always_on')
    op.drop_column('user_agents', 'system_key')
