"""employee exits

HR's record that someone is leaving (one row per person, deleted when a
resignation is withdrawn) and the log of every employment change. The exit row
is what takes a person out of assignment; see app/hr/staff.py.

Revision ID: c4e8a2f6d913
Revises: a1d5c8e3f906
Create Date: 2026-11-09 12:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'c4e8a2f6d913'
down_revision: str | None = 'a1d5c8e3f906'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'employee_exits',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('kind', sa.String(length=20), server_default='resigned', nullable=False),
        sa.Column('resigned_on', sa.Date(), nullable=True),
        sa.Column('last_working_day', sa.Date(), nullable=False),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('recorded_by_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('access_ended_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('payroll_employee_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('payroll_left_on_set', sa.Date(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['recorded_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['payroll_employee_id'], ['payroll_employees.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_employee_exits_user_id', 'employee_exits', ['user_id'], unique=True)

    op.create_table(
        'employment_events',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('actor_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('action', sa.String(length=40), nullable=False),
        sa.Column('summary', sa.Text(), nullable=False),
        sa.Column('detail', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['actor_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_employment_events_user_created', 'employment_events', ['user_id', 'created_at']
    )


def downgrade() -> None:
    op.drop_index('ix_employment_events_user_created', table_name='employment_events')
    op.drop_table('employment_events')
    op.drop_index('ix_employee_exits_user_id', table_name='employee_exits')
    op.drop_table('employee_exits')
