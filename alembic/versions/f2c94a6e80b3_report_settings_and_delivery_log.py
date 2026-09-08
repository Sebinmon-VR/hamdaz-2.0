"""reports: who they go to, and a log of what was sent

Revision ID: f2c94a6e80b3
Revises: e5b83c1f2d47
Create Date: 2026-09-08

Who a filed report went to was a constant in the code — the team's managers and
leads, the CEO, super admins. That was the right default and the wrong thing to
be unchangeable: an organisation that wants the CEO off every daily report, or
the whole of finance on the weeklies, should not need a deploy to say so.

``report_settings`` is one row of switches, and ``report_deliveries`` is the
log. The log is a table rather than only the three columns already on
``reports`` because the two answer different questions. The columns answer "did
my manager get Tuesday's?", which is about one report; the log answers "has
anything failed to send this week?", which no per-row summary can. Both are
kept.

The delivery rows copy the team and author names off the report rather than
joining to it, so a record still reads after its report is deleted — which is
exactly the record somebody is trying to look up.

Two columns on ``report_schedules`` let one team differ from the global rule.
``notify`` is deliberately nullable: null means "follow the global setting", so
turning that setting on later reaches a team that never expressed a preference
and does not reach one that said no. Two states could not express that.

None of this widens who may *read* a report. Delivery and visibility are
separate questions and only the first is configurable here.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f2c94a6e80b3"
down_revision: str | None = "e5b83c1f2d47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _stamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
    ]


def upgrade() -> None:
    emails = postgresql.ARRAY(sa.String(length=320))

    op.create_table(
        "report_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "notify_on_submit", sa.Boolean(), server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "notify_team_oversight", sa.Boolean(), server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "notify_company_wide", sa.Boolean(), server_default=sa.text("true"),
            nullable=False,
        ),
        # The same three roles that may read every report, but its own list: a
        # CEO who wants to keep the access and lose the daily mail changes this
        # and nothing else.
        sa.Column(
            "company_roles", postgresql.ARRAY(sa.String(length=40)),
            server_default=sa.text("'{super_admin,ceo,manager}'::varchar[]"),
            nullable=False,
        ),
        sa.Column(
            "extra_recipients", emails,
            server_default=sa.text("'{}'::varchar[]"), nullable=False,
        ),
        sa.Column(
            "copy_author", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "notify_cadences", postgresql.ARRAY(sa.String(length=16)),
            server_default=sa.text("'{daily,weekly,monthly,ad_hoc}'::varchar[]"),
            nullable=False,
        ),
        sa.Column(
            "max_tasks_in_email", sa.Integer(), server_default=sa.text("8"),
            nullable=False,
        ),
        sa.Column(
            "include_task_list", sa.Boolean(), server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "include_issue_list", sa.Boolean(), server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "log_retention_days", sa.Integer(), server_default=sa.text("90"),
            nullable=False,
        ),
        sa.Column(
            "updated_by_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        ),
        *_stamps(),
    )

    op.create_table(
        "report_deliveries",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # SET NULL, not CASCADE: a delivery record outlives the report it was
        # about, because that is the record somebody is looking up.
        sa.Column(
            "report_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("reports.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("team_name", sa.String(length=200), nullable=True),
        sa.Column("author_name", sa.String(length=200), nullable=True),
        sa.Column("cadence", sa.String(length=16), nullable=True),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "recipients", emails,
            server_default=sa.text("'{}'::varchar[]"), nullable=False,
        ),
        sa.Column("detail", sa.Text(), nullable=True),
        *_stamps(),
    )
    op.create_index("ix_report_deliveries_created", "report_deliveries", ["created_at"])
    op.create_index("ix_report_deliveries_report", "report_deliveries", ["report_id"])
    op.create_index("ix_report_deliveries_status", "report_deliveries", ["status"])

    # Nullable on purpose — see the note at the top.
    op.add_column("report_schedules", sa.Column("notify", sa.Boolean(), nullable=True))
    op.add_column(
        "report_schedules",
        sa.Column(
            "extra_recipients", emails,
            server_default=sa.text("'{}'::varchar[]"), nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("report_schedules", "extra_recipients")
    op.drop_column("report_schedules", "notify")
    op.drop_index("ix_report_deliveries_status", table_name="report_deliveries")
    op.drop_index("ix_report_deliveries_report", table_name="report_deliveries")
    op.drop_index("ix_report_deliveries_created", table_name="report_deliveries")
    op.drop_table("report_deliveries")
    op.drop_table("report_settings")
