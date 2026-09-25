"""VAT on the total, not on each line

The tax moves from the lines to the quote: one name and one rate, applied
once to the total before tax and rounded once. The rate the lines shared is
carried up so no priced quote loses its VAT, and the per-line columns go.

Revision ID: a7f3c1e5d928
Revises: d6e2a8b3c914
Create Date: 2026-09-25 17:10:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'a7f3c1e5d928'
down_revision: str | None = 'd6e2a8b3c914'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('quote_requests', sa.Column('tax_name', sa.String(length=60), nullable=True))
    op.add_column(
        'quote_requests', sa.Column('tax_percentage', sa.Numeric(precision=6, scale=3), nullable=True)
    )
    # The rate the lines carried, lifted to the quote: the highest where they
    # differ, which is the one the customer was being charged on the goods.
    op.execute(
        """
        UPDATE quote_requests AS q
        SET tax_percentage = s.pct,
            tax_name = COALESCE(s.name, 'VAT')
        FROM (
            SELECT request_id,
                   MAX(tax_percentage) AS pct,
                   MAX(tax_name) AS name
            FROM quote_request_items
            WHERE tax_percentage IS NOT NULL
            GROUP BY request_id
        ) AS s
        WHERE q.id = s.request_id
        """
    )
    op.drop_column('quote_request_items', 'tax_percentage')
    op.drop_column('quote_request_items', 'tax_name')


def downgrade() -> None:
    op.add_column(
        'quote_request_items', sa.Column('tax_name', sa.String(length=60), nullable=True)
    )
    op.add_column(
        'quote_request_items',
        sa.Column('tax_percentage', sa.Numeric(precision=6, scale=3), nullable=True),
    )
    op.execute(
        """
        UPDATE quote_request_items AS i
        SET tax_percentage = q.tax_percentage,
            tax_name = q.tax_name
        FROM quote_requests AS q
        WHERE i.request_id = q.id AND q.tax_percentage IS NOT NULL
        """
    )
    op.drop_column('quote_requests', 'tax_percentage')
    op.drop_column('quote_requests', 'tax_name')
