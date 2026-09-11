"""projects, and the status reports filed about them

Revision ID: d9f3b28c47e1
Revises: c4a71b6de902
Create Date: 2026-09-10

Eight tables and one alteration. The shape is explained at length in
``app/models/project.py`` and ``app/models/report.py``; what is worth saying
here is why the parts are split this way and what the alteration is guarding.

**Six tables for the module itself.** A project, the people on it, its
milestones, its tasks, its issues, and a log of every movement. The log is the
one that would not be obvious: a single ``percent_complete`` column answers
"where is this now" and nothing else, and every question a weekly or monthly
report exists to answer is about a *window of time*. ``project_updates`` is
what makes day-wise, week-wise, monthly and yearly reporting the same range
query with different bounds rather than four separate features.

**Two tables for what a report says about them.** ``report_project_lines``
snapshots a project's health, percentage and counts as they stood when a report
was filed, and ``report_milestone_lines`` does the same for its timeline. Both
are copies rather than joins, exactly as ``report_task_lines`` is: a project
that goes green next month must not silently rewrite the report that said it
was red. The foreign keys back to the live rows are ``SET NULL``, so deleting a
project leaves every report filed about it intact and readable.

**The alteration, and the NULL that makes it necessary.** ``reports`` gains a
``scope`` and a nullable ``project_id``. Its old uniqueness — one report per
team, author, cadence and period — cannot simply absorb the new column, because
Postgres treats NULLs as distinct in a unique index: every team report has a
null project, so a single combined constraint would stop guarding them
entirely and two daily reports for the same Tuesday would sail through. So the
constraint is dropped and replaced by two partial unique indexes, split on
whether a project is named:

  * ``project_id IS NULL`` — keyed on team, author, cadence, period **and
    scope**, so somebody can file both their own weekly and the team's
    portfolio weekly in the same week. Those are different reports about
    different things;
  * ``project_id IS NOT NULL`` — keyed on project, author, cadence and period,
    so the same person can report on three projects in one week while still
    being stopped from reporting twice on one of them.

Existing rows are backfilled to ``scope = 'team'`` by the column default, which
is what every report filed before this migration was, so nothing changes
meaning. The downgrade restores the original constraint; it will fail if
project reports have been filed, and deliberately — silently dropping somebody's
status reports to move a schema backwards is not a thing a downgrade should do
quietly.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d9f3b28c47e1"
down_revision: str | None = "c4a71b6de902"
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


def _uuid(name: str, target: str, ondelete: str, *, nullable: bool = True) -> sa.Column:
    return sa.Column(
        name,
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey(target, ondelete=ondelete),
        nullable=nullable,
    )


def _count(name: str) -> sa.Column:
    return sa.Column(name, sa.Integer(), server_default=sa.text("0"), nullable=False)


def upgrade() -> None:
    # ── the module ─────────────────────────────────────────────────────
    op.create_table(
        "projects",
        _pk(),
        _uuid("team_id", "teams.id", "CASCADE", nullable=False),
        sa.Column("code", sa.String(length=32)),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("objective", sa.Text()),
        sa.Column(
            "status", sa.String(length=16),
            server_default=sa.text("'planned'"), nullable=False,
        ),
        _uuid("lead_id", "users.id", "SET NULL"),
        sa.Column("start_on", sa.Date()),
        sa.Column("target_end_on", sa.Date()),
        sa.Column("actual_end_on", sa.Date()),
        # Every dial starts grey, and that is the most important default here.
        # A project nobody has assessed showing green is indistinguishable at a
        # glance from a project that is fine, and glancing is the whole point
        # of a portfolio board.
        *[
            sa.Column(
                f"rag_{dial}", sa.String(length=8),
                server_default=sa.text("'grey'"), nullable=False,
            )
            for dial in ("overall", "scope", "cost", "schedule", "benefits")
        ],
        *[
            sa.Column(
                f"trend_{dial}", sa.String(length=12),
                server_default=sa.text("'steady'"), nullable=False,
            )
            for dial in ("overall", "scope", "cost", "schedule", "benefits")
        ],
        sa.Column("health_reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("health_note", sa.Text()),
        # Nullable: null means "use what the tasks say". Two columns would let
        # a stored and a derived figure disagree with no way to tell which was
        # meant; nullable-with-fallback makes the override explicit.
        sa.Column("percent_complete", sa.Integer()),
        sa.Column("budget_amount", sa.Numeric(14, 2)),
        sa.Column("spend_amount", sa.Numeric(14, 2)),
        sa.Column(
            "currency", sa.String(length=3),
            server_default=sa.text("'AED'"), nullable=False,
        ),
        sa.Column(
            "extra", postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"), nullable=False,
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        _uuid("created_by_id", "users.id", "SET NULL"),
        *_stamps(),
        # Unique within a team only. Two teams both wanting "PH-1" is not a
        # conflict worth refusing.
        sa.UniqueConstraint("team_id", "code", name="uq_project_team_code"),
        sa.CheckConstraint(
            "percent_complete IS NULL OR (percent_complete >= 0 AND percent_complete <= 100)",
            name="ck_project_percent_range",
        ),
    )
    op.create_index("ix_projects_team_id", "projects", ["team_id"])
    op.create_index("ix_projects_team_status", "projects", ["team_id", "status"])
    op.create_index("ix_projects_lead", "projects", ["lead_id"])
    op.create_index("ix_projects_rag_overall", "projects", ["rag_overall"])
    op.create_index("ix_projects_archived_at", "projects", ["archived_at"])

    op.create_table(
        "project_members",
        _pk(),
        _uuid("project_id", "projects.id", "CASCADE", nullable=False),
        _uuid("user_id", "users.id", "CASCADE", nullable=False),
        sa.Column(
            "role", sa.String(length=16),
            server_default=sa.text("'member'"), nullable=False,
        ),
        sa.Column("responsibility", sa.String(length=200)),
        _uuid("added_by_id", "users.id", "SET NULL"),
        *_stamps(),
        # One row per person, unlike team_memberships. A project has one
        # relationship to each person: being both lead and member of the same
        # project is not a distinction anything acts on, and allowing it would
        # make "who leads this" a query with more than one answer.
        sa.UniqueConstraint("project_id", "user_id", name="uq_project_member"),
    )
    op.create_index("ix_project_members_project_id", "project_members", ["project_id"])
    # "Every project this person is on" — the query behind their own dashboard.
    op.create_index("ix_project_members_user", "project_members", ["user_id"])

    op.create_table(
        "project_milestones",
        _pk(),
        _uuid("project_id", "projects.id", "CASCADE", nullable=False),
        sa.Column("position", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("detail", sa.Text()),
        _uuid("owner_id", "users.id", "SET NULL"),
        sa.Column("start_on", sa.Date()),
        sa.Column("due_on", sa.Date()),
        sa.Column("done_on", sa.Date()),
        # Set once, on the first date the milestone ever had, and never again.
        # "How far has this slipped" stays answerable after the third
        # reschedule — which is the point at which somebody starts asking.
        sa.Column("baseline_due_on", sa.Date()),
        _count("percent_complete"),
        sa.Column(
            "plan", sa.String(length=24),
            server_default=sa.text("'on_plan'"), nullable=False,
        ),
        sa.Column("is_key", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        *_stamps(),
        sa.CheckConstraint(
            "percent_complete >= 0 AND percent_complete <= 100",
            name="ck_milestone_percent_range",
        ),
    )
    op.create_index(
        "ix_project_milestones_project", "project_milestones", ["project_id", "position"]
    )
    op.create_index("ix_project_milestones_due", "project_milestones", ["due_on"])

    op.create_table(
        "project_tasks",
        _pk(),
        _uuid("project_id", "projects.id", "CASCADE", nullable=False),
        # SET NULL, not CASCADE: deleting a milestone is a re-plan, and the
        # work underneath it is usually the reason for it.
        _uuid("milestone_id", "project_milestones.id", "SET NULL"),
        sa.Column("position", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("detail", sa.Text()),
        _uuid("assignee_id", "users.id", "SET NULL"),
        sa.Column(
            "status", sa.String(length=16),
            server_default=sa.text("'not_started'"), nullable=False,
        ),
        sa.Column(
            "priority", sa.String(length=12),
            server_default=sa.text("'medium'"), nullable=False,
        ),
        _count("percent_complete"),
        sa.Column("start_on", sa.Date()),
        sa.Column("due_on", sa.Date()),
        sa.Column("done_at", sa.DateTime(timezone=True)),
        sa.Column("estimate_hours", sa.Numeric(8, 2)),
        sa.Column("spent_hours", sa.Numeric(8, 2)),
        sa.Column("blocked_reason", sa.Text()),
        _uuid("created_by_id", "users.id", "SET NULL"),
        *_stamps(),
        sa.CheckConstraint(
            "percent_complete >= 0 AND percent_complete <= 100",
            name="ck_task_percent_range",
        ),
    )
    op.create_index("ix_project_tasks_project", "project_tasks", ["project_id", "position"])
    op.create_index("ix_project_tasks_assignee_id", "project_tasks", ["assignee_id"])
    # "My open work, soonest first" — the query behind every person's own page,
    # and the one that has to stay fast as the table grows.
    op.create_index(
        "ix_project_tasks_assignee_status",
        "project_tasks",
        ["assignee_id", "status", "due_on"],
    )
    op.create_index("ix_project_tasks_milestone", "project_tasks", ["milestone_id"])
    op.create_index("ix_project_tasks_done_at", "project_tasks", ["done_at"])

    op.create_table(
        "project_issues",
        _pk(),
        _uuid("project_id", "projects.id", "CASCADE", nullable=False),
        sa.Column("position", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("detail", sa.Text()),
        sa.Column(
            "status", sa.String(length=16),
            server_default=sa.text("'open'"), nullable=False,
        ),
        sa.Column(
            "priority", sa.String(length=12),
            server_default=sa.text("'medium'"), nullable=False,
        ),
        _uuid("owner_id", "users.id", "SET NULL"),
        _uuid("raised_by_id", "users.id", "SET NULL"),
        sa.Column("raised_on", sa.Date(), nullable=False),
        sa.Column("due_on", sa.Date()),
        sa.Column("resolved_on", sa.Date()),
        # A column rather than a severity level: "serious" and "needs somebody
        # above me" are genuinely different claims, and only the second one
        # belongs in the support-needed box on a status report.
        sa.Column(
            "needs_support", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("support_note", sa.Text()),
        *_stamps(),
    )
    op.create_index("ix_project_issues_project", "project_issues", ["project_id", "position"])
    op.create_index("ix_project_issues_status", "project_issues", ["status"])
    op.create_index("ix_project_issues_raised", "project_issues", ["raised_on"])

    op.create_table(
        "project_updates",
        _pk(),
        _uuid("project_id", "projects.id", "CASCADE", nullable=False),
        # SET NULL throughout, so the history of a deleted task survives as
        # project history. Somebody deleting a task must not silently rewrite
        # what last month's report was based on.
        _uuid("task_id", "project_tasks.id", "SET NULL"),
        _uuid("milestone_id", "project_milestones.id", "SET NULL"),
        _uuid("issue_id", "project_issues.id", "SET NULL"),
        _uuid("author_id", "users.id", "SET NULL"),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("subject", sa.String(length=500)),
        sa.Column("percent_before", sa.Integer()),
        sa.Column("percent_after", sa.Integer()),
        sa.Column("status_before", sa.String(length=16)),
        sa.Column("status_after", sa.String(length=16)),
        sa.Column("hours", sa.Numeric(8, 2)),
        sa.Column("body", sa.Text()),
        *_stamps(),
    )
    # Every read is "this project, over this window, newest first".
    op.create_index(
        "ix_project_updates_project_created", "project_updates", ["project_id", "created_at"]
    )
    op.create_index("ix_project_updates_task", "project_updates", ["task_id"])
    op.create_index(
        "ix_project_updates_author", "project_updates", ["author_id", "created_at"]
    )
    op.create_index("ix_project_updates_kind", "project_updates", ["kind"])

    # ── what a report says about them ──────────────────────────────────
    op.create_table(
        "report_project_lines",
        _pk(),
        _uuid("report_id", "reports.id", "CASCADE", nullable=False),
        sa.Column("position", sa.Integer(), server_default=sa.text("0"), nullable=False),
        # SET NULL: deleting a project leaves the report intact. Everything
        # below is a copy, so the row still reads afterwards.
        _uuid("project_id", "projects.id", "SET NULL"),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("code", sa.String(length=32)),
        sa.Column("lead_name", sa.String(length=200)),
        sa.Column("status", sa.String(length=16)),
        sa.Column("start_on", sa.Date()),
        sa.Column("target_end_on", sa.Date()),
        # Plain columns rather than JSONB, because the whole point of a RAG
        # history is querying it: "show me every project that went red this
        # quarter" is the first question anybody asks of a set of these.
        *[sa.Column(f"rag_{d}", sa.String(length=8)) for d in
          ("overall", "scope", "cost", "schedule", "benefits")],
        *[sa.Column(f"trend_{d}", sa.String(length=12)) for d in
          ("overall", "scope", "cost", "schedule", "benefits")],
        _count("percent_complete"),
        _count("tasks_total"),
        _count("tasks_done"),
        _count("tasks_open"),
        _count("tasks_blocked"),
        _count("tasks_overdue"),
        _count("milestones_total"),
        _count("milestones_done"),
        _count("milestones_overdue"),
        _count("issues_open"),
        # Movement inside the window this report covers, rather than the
        # running total. What makes a weekly report about the week.
        _count("updates_in_period"),
        _count("tasks_completed_in_period"),
        sa.Column("budget_amount", sa.Numeric(14, 2)),
        sa.Column("spend_amount", sa.Numeric(14, 2)),
        sa.Column("currency", sa.String(length=3)),
        sa.Column("activities", sa.Text()),
        sa.Column("action_required", sa.Text()),
        sa.Column("note", sa.Text()),
        *_stamps(),
    )
    op.create_index(
        "ix_report_project_lines_report", "report_project_lines", ["report_id", "position"]
    )
    op.create_index(
        "ix_report_project_lines_project", "report_project_lines", ["project_id"]
    )
    op.create_index(
        "ix_report_project_lines_rag_overall", "report_project_lines", ["rag_overall"]
    )

    op.create_table(
        "report_milestone_lines",
        _pk(),
        _uuid("project_line_id", "report_project_lines.id", "CASCADE", nullable=False),
        sa.Column("position", sa.Integer(), server_default=sa.text("0"), nullable=False),
        _uuid("milestone_id", "project_milestones.id", "SET NULL"),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("owner_name", sa.String(length=200)),
        sa.Column("start_on", sa.Date()),
        sa.Column("due_on", sa.Date()),
        sa.Column("done_on", sa.Date()),
        sa.Column("baseline_due_on", sa.Date()),
        _count("percent_complete"),
        sa.Column("plan", sa.String(length=24)),
        # Stored rather than recomputed on read: "overdue" is relative to a
        # date, and recomputing it later would make a report filed in March
        # describe itself differently in June.
        sa.Column("state", sa.String(length=16)),
        sa.Column("is_key", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("note", sa.Text()),
        *_stamps(),
    )
    op.create_index(
        "ix_report_milestone_lines_line",
        "report_milestone_lines",
        ["project_line_id", "position"],
    )

    # ── reports learn what they are about ──────────────────────────────
    op.add_column(
        "reports",
        sa.Column(
            "scope", sa.String(length=16),
            server_default=sa.text("'team'"), nullable=False,
        ),
    )
    op.add_column(
        "reports",
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_reports_project", "reports", "projects", ["project_id"], ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_reports_scope", "reports", ["scope"])
    op.create_index("ix_reports_project", "reports", ["project_id", "period_start"])

    # The swap. The old constraint cannot absorb project_id — Postgres treats
    # NULLs as distinct, so every team report would become trivially unique and
    # the guard would silently stop working. Two partial indexes instead, one
    # for each side of "is a project named".
    op.drop_constraint("uq_report_author_period", "reports", type_="unique")
    op.create_index(
        "uq_report_author_period",
        "reports",
        ["team_id", "author_id", "cadence", "period_start", "scope"],
        unique=True,
        postgresql_where=sa.text("project_id IS NULL"),
    )
    op.create_index(
        "uq_report_project_period",
        "reports",
        ["project_id", "author_id", "cadence", "period_start"],
        unique=True,
        postgresql_where=sa.text("project_id IS NOT NULL"),
    )


def downgrade() -> None:
    # Restores the original constraint. This will fail if any project report
    # has been filed, and deliberately: quietly dropping somebody's status
    # reports to move the schema backwards is not what a downgrade is for.
    # Delete them first if that is genuinely what is wanted.
    op.drop_index("uq_report_project_period", table_name="reports")
    op.drop_index("uq_report_author_period", table_name="reports")
    op.create_unique_constraint(
        "uq_report_author_period",
        "reports",
        ["team_id", "author_id", "cadence", "period_start"],
    )
    op.drop_index("ix_reports_project", table_name="reports")
    op.drop_index("ix_reports_scope", table_name="reports")
    op.drop_constraint("fk_reports_project", "reports", type_="foreignkey")
    op.drop_column("reports", "project_id")
    op.drop_column("reports", "scope")

    op.drop_table("report_milestone_lines")
    op.drop_table("report_project_lines")
    op.drop_table("project_updates")
    op.drop_table("project_issues")
    op.drop_table("project_tasks")
    op.drop_table("project_milestones")
    op.drop_table("project_members")
    op.drop_table("projects")
