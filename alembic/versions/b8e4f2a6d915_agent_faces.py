"""agent faces and RFQs: a PNG of each agent's avatar for its Teams cards,
and each RFQ an agent sent, watched for the supplier's reply

Revision ID: b8e4f2a6d915
Revises: a7d3e9f1c204
Create Date: 2026-10-15 21:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'b8e4f2a6d915'
down_revision: str | None = 'a7d3e9f1c204'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('user_agents', sa.Column('face_png', sa.LargeBinary(), nullable=True))
    op.add_column('user_agents', sa.Column('face_key', sa.String(length=120), nullable=True))
    op.create_table(
        'agent_rfqs',
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('run_id', sa.UUID(), nullable=False),
        sa.Column('agent_id', sa.UUID(), nullable=False),
        sa.Column('owner_id', sa.UUID(), nullable=False),
        sa.Column('task_id', sa.String(length=64), nullable=True),
        sa.Column('task_title', sa.Text(), nullable=True),
        sa.Column('supplier_name', sa.Text(), nullable=True),
        sa.Column('supplier_email', sa.String(length=320), nullable=False),
        sa.Column('sent_to', sa.String(length=320), nullable=False),
        sa.Column('subject', sa.Text(), nullable=False),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('status', sa.String(length=16), server_default=sa.text("'waiting'"), nullable=False),
        sa.Column('reply', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('quote_request_id', sa.UUID(), nullable=True),
        sa.Column('report', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['run_id'], ['agent_runs.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['agent_id'], ['user_agents.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['quote_request_id'], ['quote_requests.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_agent_rfqs_run', 'agent_rfqs', ['run_id'])
    op.create_index('ix_agent_rfqs_status', 'agent_rfqs', ['status'])


def downgrade() -> None:
    op.drop_index('ix_agent_rfqs_status', table_name='agent_rfqs')
    op.drop_index('ix_agent_rfqs_run', table_name='agent_rfqs')
    op.drop_table('agent_rfqs')
    op.drop_column('user_agents', 'face_key')
    op.drop_column('user_agents', 'face_png')
