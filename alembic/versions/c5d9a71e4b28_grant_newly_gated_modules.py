"""grant leave, quotes and work assignment to every existing team

Three modules have just started enforcing their team grant: ``leave``,
``quotes`` and ``assignment``. Until now their routers took a bare session and
the frontend listed them unconditionally, so being "switched off" for a team did
nothing at all — which is what made the module access screen look broken.

Turning the gate on without this migration would lock people out, because the
grants were never needed and so were never made. Before this runs:

* ``quotes`` is granted to **no team** — everybody would lose it.
* ``leave`` is missing on two of the six active teams.
* ``assignment`` is granted to one team.

And the bypass is narrower than it looks: ``effective_access`` hands everything
to ``super_admin`` alone, so the CEO and managers would be refused too.

So this grants all three to every team that is not archived, which reproduces
exactly what everybody could reach the day before. Nothing is taken away here
and nobody gains anything they did not already have — the switches simply start
meaning what they say, and an administrator can now turn one off and have it
stay off.

Deliberately only *active* teams. An archived team's grants are not worth
recreating, and a team that is un-archived later is a decision somebody makes
with the access screen open.

``ON CONFLICT DO NOTHING`` so a team that already holds one keeps its row —
including its ``all_pages`` setting and who granted it, which are worth more
than a uniform rewrite.

Revision ID: c5d9a71e4b28
Revises: a3f81c6b24d9
Create Date: 2026-09-16 16:40:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'c5d9a71e4b28'
down_revision: str | None = 'a3f81c6b24d9'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: The three that have just started gating.
NEWLY_GATED: tuple[str, ...] = ("leave", "quotes", "assignment")


def upgrade() -> None:
    # Guarded on the module actually existing in the catalogue table. The
    # modules are seeded from code at startup, and a database that has not
    # seen that yet would otherwise fail on the foreign key rather than
    # simply having nothing to grant.
    op.execute(
        sa.text(
            """
            INSERT INTO team_module_access (team_id, module_key, all_pages)
            SELECT t.id, m.key, true
            FROM teams t
            CROSS JOIN modules m
            WHERE t.archived_at IS NULL
              AND m.key = ANY(:keys)
            ON CONFLICT (team_id, module_key) DO NOTHING
            """
        ).bindparams(sa.bindparam("keys", value=list(NEWLY_GATED)))
    )


def downgrade() -> None:
    # Deliberately does nothing.
    #
    # The rows this created are indistinguishable from grants an administrator
    # made by hand afterwards, and deleting them all would silently revoke real
    # decisions. Leaving them costs nothing: with the gate removed the grants
    # are simply unread, which is the state the system was in before.
    pass
