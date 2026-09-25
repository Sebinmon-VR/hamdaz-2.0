"""intake: clear the log once, before the first live run

Revision ID: f4c8a2d91e37
Revises: e3b7d9a4c216
Create Date: 2026-09-23

Every row in the intake log today comes from the simulated runs of early
September — 50 of them — and the person switching the intake on wants the
screen to hold only what arrives from that moment. Asked for once, not as a
rule: switching on later keeps its history and only sets aside messages
recorded but never decided (see ``app.intake.service.start_from_now``). So
the one-off lives here, where it runs exactly once per database and is
written down, rather than as a hand-typed statement nobody can find later.

Deleted rows cannot be restored, so the downgrade does nothing.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "f4c8a2d91e37"
down_revision: str | None = "e3b7d9a4c216"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DELETE FROM intake_messages")


def downgrade() -> None:
    # The rows are gone; there is nothing to put back.
    pass
