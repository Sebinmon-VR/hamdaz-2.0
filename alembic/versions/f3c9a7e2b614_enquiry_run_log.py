"""enquiry run log

The analysis page shows what a run is doing as it does it: one JSONB column
holding the last run's steps, written as the run goes. See app/enquiries.

Revision ID: f3c9a7e2b614
Revises: e8b3c5d1f402
Create Date: 2026-10-06 12:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'f3c9a7e2b614'
down_revision: str | None = 'e8b3c5d1f402'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('enquiry_analyses', sa.Column('run_log', postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column('enquiry_analyses', 'run_log')
