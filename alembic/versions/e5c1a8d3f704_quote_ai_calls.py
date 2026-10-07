"""quote ai calls

Each call to a text model made while reading a quote's documents — which
provider and model, the tokens, and what it cost — so the quote page can say.

Revision ID: e5c1a8d3f704
Revises: d8b3e6f1a492
Create Date: 2026-10-07 16:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'e5c1a8d3f704'
down_revision: str | None = 'd8b3e6f1a492'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'quote_ai_calls',
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('request_id', sa.UUID(), nullable=False),
        sa.Column('user_id', sa.UUID(), nullable=True),
        sa.Column('purpose', sa.String(length=40), nullable=False),
        sa.Column('label', sa.Text(), nullable=True),
        sa.Column('provider', sa.String(length=40), nullable=False),
        sa.Column('model', sa.String(length=120), nullable=False),
        sa.Column('input_tokens', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('output_tokens', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('cost_usd', sa.Numeric(precision=12, scale=6), server_default=sa.text('0'), nullable=False),
        sa.Column('used', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['request_id'], ['quote_requests.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_quote_ai_calls_request', 'quote_ai_calls', ['request_id', 'created_at'])


def downgrade() -> None:
    op.drop_index('ix_quote_ai_calls_request', table_name='quote_ai_calls')
    op.drop_table('quote_ai_calls')
