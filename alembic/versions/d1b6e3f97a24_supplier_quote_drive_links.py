"""remember where a supplier quote was filed in OneDrive

The documents people upload against a quote are stored in the database and stay
there — that remains the system of record. This adds somewhere to record that a
copy was also filed to a drive folder, so a colleague can open the supplier's
own PDF without an account on this system.

Both nullable, and they stay null in the ordinary case: filing is off unless a
drive is configured, and a failed upload deliberately does not fail the attach.
See ``app/quoting/storage.py``.

Revision ID: d1b6e3f97a24
Revises: c5d9a71e4b28
Create Date: 2026-09-16 17:30:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'd1b6e3f97a24'
down_revision: str | None = 'c5d9a71e4b28'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "supplier_quotes", sa.Column("drive_item_id", sa.String(length=120), nullable=True)
    )
    op.add_column("supplier_quotes", sa.Column("drive_url", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("supplier_quotes", "drive_url")
    op.drop_column("supplier_quotes", "drive_item_id")
