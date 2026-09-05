"""the job posting form: an opening's advert is a template, not columns

Revision ID: d4e2b8c1f077
Revises: c7d1a4b90e33
Create Date: 2026-09-05

A separate migration rather than an edit to c7d1a4b90e33, which had already
been applied. Editing a migration that has run leaves every database that ran
it with a schema the code no longer expects, and nothing detects that: alembic
reports the revision as current, because it is, and the mismatch only surfaces
as a 500 on the first query that names a missing column.

The three columns are nullable or defaulted, so this runs against a table with
rows in it without needing a backfill. An opening that predates the posting
form has ``posting_template_id`` null, which is what "written straight onto the
columns rather than through a form" means everywhere else in the module.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d4e2b8c1f077"
down_revision: str | None = "c7d1a4b90e33"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "job_openings",
        sa.Column("posting_template_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "job_openings",
        sa.Column(
            "posting_template_version",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
    )
    op.add_column(
        "job_openings",
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    # RESTRICT, like the application form's: deleting a template that an advert
    # was written against would leave that advert unreadable.
    op.create_foreign_key(
        "fk_job_openings_posting_template_id",
        "job_openings",
        "form_templates",
        ["posting_template_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_job_openings_posting_template_id", "job_openings", type_="foreignkey"
    )
    op.drop_column("job_openings", "details")
    op.drop_column("job_openings", "posting_template_version")
    op.drop_column("job_openings", "posting_template_id")
