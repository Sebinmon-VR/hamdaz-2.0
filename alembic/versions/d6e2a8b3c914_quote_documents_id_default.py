"""quote documents: the id generates itself

The quote_documents table was created without the server default every other
table's id carries, so the first real upload arrived with a null primary key
and Postgres refused it. The model has always said ``gen_random_uuid()``; the
test database, built from the model, had it; the migrated one did not. This
gives the column the default it was meant to have.

Revision ID: d6e2a8b3c914
Revises: c5d1f7a2e803
Create Date: 2026-09-25 13:30:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'd6e2a8b3c914'
down_revision: str | None = 'c5d1f7a2e803'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        'quote_documents',
        'id',
        server_default=sa.text('gen_random_uuid()'),
        existing_type=sa.UUID(),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        'quote_documents',
        'id',
        server_default=None,
        existing_type=sa.UUID(),
        existing_nullable=False,
    )
