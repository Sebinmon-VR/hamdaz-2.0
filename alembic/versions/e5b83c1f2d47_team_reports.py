"""team reports: what each team files, and what it said

Revision ID: e5b83c1f2d47
Revises: d1a6e37c40b8
Create Date: 2026-09-08

Seven tables. The shape is explained at length in ``app/models/report.py``;
what is worth saying here is why the parts are split the way they are.

``report_schedules`` is what makes each team's report different from the next
team's: it points one team's cadence at one ``form_templates`` row. The
template holds the questions, this holds which team is asked them, and the six
sections every report has are in code because their whole value is being the
same everywhere.

Tasks, issues and metrics get tables rather than JSONB columns because the
questions people ask of reports are across them and not within one — what is
blocking presales this month, how the conversion rate moved, which bid has been
sitting on somebody's report for a fortnight. Prose stays prose; nobody queries
it.

``report_task_lines`` snapshots what the Proposals list said at the time. The
list is live and a report that changes after it is filed is not a report. The
links stay live, so the current state is one click away. Nothing here ever
writes back to SharePoint.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e5b83c1f2d47"
down_revision: str | None = "d1a6e37c40b8"
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


def _pk() -> sa.Column:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )


def upgrade() -> None:
    op.create_table(
        "report_schedules",
        _pk(),
        sa.Column(
            "team_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("teams.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("cadence", sa.String(length=16), nullable=False),
        # RESTRICT, not CASCADE: a template a team is actively filing against
        # must not vanish because somebody tidied up the templates screen.
        sa.Column(
            "template_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("form_templates.id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("due_hour", sa.Integer(), server_default=sa.text("18"), nullable=False),
        sa.Column("due_weekday", sa.Integer(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_by_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        ),
        *_stamps(),
        sa.UniqueConstraint("team_id", "cadence", name="uq_report_schedule_team_cadence"),
    )
    op.create_index("ix_report_schedules_team_id", "report_schedules", ["team_id"])

    op.create_table(
        "reports",
        _pk(),
        sa.Column(
            "team_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("teams.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "author_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "template_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("form_templates.id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column(
            "template_version", sa.Integer(), server_default=sa.text("1"), nullable=False
        ),
        sa.Column("cadence", sa.String(length=16), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column(
            "status", sa.String(length=16), server_default=sa.text("'draft'"), nullable=False
        ),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "notified_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("notify_error", sa.Text(), nullable=True),
        sa.Column("overview", sa.Text(), nullable=True),
        sa.Column("remarks", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column(
            "answers", postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"), nullable=False,
        ),
        *_stamps(),
        # Two daily reports for the same Tuesday is a mistake every time.
        sa.UniqueConstraint(
            "team_id", "author_id", "cadence", "period_start",
            name="uq_report_author_period",
        ),
    )
    op.create_index("ix_reports_team_period", "reports", ["team_id", "period_start"])
    op.create_index("ix_reports_author_period", "reports", ["author_id", "period_start"])
    op.create_index("ix_reports_status", "reports", ["status"])

    op.create_table(
        "report_task_lines",
        _pk(),
        sa.Column(
            "report_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("reports.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column(
            "source", sa.String(length=16), server_default=sa.text("'manual'"), nullable=False
        ),
        sa.Column("external_id", sa.String(length=64), nullable=True),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=80), nullable=True),
        sa.Column(
            "completion", sa.String(length=24),
            server_default=sa.text("'in_progress'"), nullable=False,
        ),
        sa.Column("percent_complete", sa.Integer(), nullable=True),
        sa.Column("priority", sa.String(length=40), nullable=True),
        sa.Column("end_user", sa.String(length=200), nullable=True),
        sa.Column("quote_no", sa.String(length=80), nullable=True),
        sa.Column("deadline", sa.Date(), nullable=True),
        sa.Column("link", sa.Text(), nullable=True),
        sa.Column("attachments_url", sa.Text(), nullable=True),
        sa.Column(
            "has_attachments", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("note", sa.Text(), nullable=True),
        *_stamps(),
    )
    op.create_index(
        "ix_report_task_lines_report", "report_task_lines", ["report_id", "position"]
    )
    op.create_index(
        "ix_report_task_lines_external_id", "report_task_lines", ["external_id"]
    )

    op.create_table(
        "report_issues",
        _pk(),
        sa.Column(
            "report_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("reports.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column(
            "severity", sa.String(length=16),
            server_default=sa.text("'medium'"), nullable=False,
        ),
        sa.Column("waiting_on", sa.String(length=200), nullable=True),
        sa.Column(
            "resolved", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "task_line_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("report_task_lines.id", ondelete="SET NULL"), nullable=True,
        ),
        *_stamps(),
    )
    op.create_index("ix_report_issues_report", "report_issues", ["report_id", "position"])

    op.create_table(
        "report_metrics",
        _pk(),
        sa.Column(
            "report_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("reports.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=160), nullable=False),
        sa.Column("unit", sa.String(length=24), nullable=True),
        # Both figures are kept. Storing only the final one would lose the more
        # interesting fact — that somebody corrected it.
        sa.Column("computed", sa.Numeric(14, 2), nullable=True),
        sa.Column("value", sa.Numeric(14, 2), nullable=True),
        sa.Column("target", sa.Numeric(14, 2), nullable=True),
        *_stamps(),
        sa.UniqueConstraint("report_id", "key", name="uq_report_metric_key"),
    )
    op.create_index("ix_report_metrics_key", "report_metrics", ["key"])

    op.create_table(
        "report_comments",
        _pk(),
        sa.Column(
            "report_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("reports.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "author_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("body", sa.Text(), nullable=False),
        *_stamps(),
    )
    op.create_index(
        "ix_report_comments_report", "report_comments", ["report_id", "created_at"]
    )

    op.create_table(
        "report_reads",
        _pk(),
        sa.Column(
            "report_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("reports.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "read_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        *_stamps(),
        sa.UniqueConstraint("report_id", "user_id", name="uq_report_read"),
    )


def downgrade() -> None:
    op.drop_table("report_reads")
    op.drop_table("report_comments")
    op.drop_table("report_metrics")
    op.drop_table("report_issues")
    op.drop_table("report_task_lines")
    op.drop_table("reports")
    op.drop_index("ix_report_schedules_team_id", table_name="report_schedules")
    op.drop_table("report_schedules")
