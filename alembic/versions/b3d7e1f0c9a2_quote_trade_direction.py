"""quote freight form

The freight form on a quote request: which way the goods cross the border, the
currency its charges are in, and the freight, documentation and duty typed by
a person.


Import, export or local, said by a person on the quote request. Until now the
question was answered only by reading the documents — an Incoterm on the
supplier's offer, a courier named on the route — and a reading that is wrong
puts duty on a local purchase or leaves it off an import with nobody able to
correct it short of editing the supplier's words. The column is the
correction: null means "the documents decide", anything else is a person's
word and wins.

Revision ID: b3d7e1f0c9a2
Revises: a7f3c1e5d928
Create Date: 2026-09-28 12:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'b3d7e1f0c9a2'
down_revision: str | None = 'a7f3c1e5d928'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'quote_requests',
        sa.Column('trade_direction', sa.String(length=12), nullable=True),
    )
    op.add_column('quote_requests', sa.Column('freight_currency', sa.String(length=3), nullable=True))
    for column in ('freight_charges', 'documentation_charges', 'duty_charges'):
        op.add_column(
            'quote_requests', sa.Column(column, sa.Numeric(18, 4), nullable=True)
        )


def downgrade() -> None:
    for column in ('duty_charges', 'documentation_charges', 'freight_charges', 'freight_currency'):
        op.drop_column('quote_requests', column)
    op.drop_column('quote_requests', 'trade_direction')
