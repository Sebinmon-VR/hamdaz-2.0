"""Telling people a report has been filed.

Sent as its author, from their own mailbox, so it arrives from a colleague and
a reply reaches the person who wrote it rather than a no-reply address. Same
transport and same reasoning as the quote-approval mail — see
``app.core.mail``.

**The message is a summary, not the report.** Enough to know whether to open
it: the period, how the work went, and anything blocked, with a link. A manager
of six people gets six of these a day, and one that pasted the whole report in
would be one nobody reads past the first. What earns its place in the body is
what would make somebody click.

**Blocked issues lead.** They are the reason to read a report today rather than
on Friday, and burying them under a task count would waste the one message that
had the reader's attention.

A failure to send never fails the submission. The report is filed as far as the
system is concerned; the email is a notification, not the transaction. What
went wrong is stored on the row so nobody has to guess whether it went.
"""

from __future__ import annotations

from datetime import date
from html import escape
from typing import Any

from app.core.mail import GraphMailer
from app.models.report import IssueSeverity, Report, ReportScope
from app.reports.catalogue import COMPLETION_LABELS, period_label

#: Beyond this the message stops being a summary. A report with forty tasks is
#: a report to open, not one to read in an inbox.
_MAX_TASKS: int = 8
_MAX_ISSUES: int = 6


def _subject(report: Report) -> str:
    who = report.author.display_name if report.author else "Somebody"
    period = period_label(report.cadence, report.period_start, report.period_end)
    blocked = [i for i in report.issues if not i.resolved and i.severity == IssueSeverity.BLOCKED]

    # A status report is about a project, and the project's name is what makes
    # the message findable in an inbox six weeks later. For a red project the
    # colour leads instead of the blocked count: it is the stronger signal and
    # there is only room for one.
    if report.scope == ReportScope.PROJECT and report.project_lines:
        line = report.project_lines[0]
        subject = f"{line.name} — {report.cadence} status — {period}"
        if line.rag_overall in ("red", "amber"):
            return f"[{line.rag_overall.upper()}] {subject}"
        flag = f" — {len(blocked)} blocked" if blocked else ""
        return f"{subject}{flag}"

    if report.scope == ReportScope.PORTFOLIO:
        team = report.team.name if report.team else "Portfolio"
        red = sum(1 for p in report.project_lines if p.rag_overall == "red")
        flag = f" — {red} red" if red else ""
        return f"{team} portfolio — {period}{flag}"

    # The one thing worth putting in a subject line, because it is the one
    # thing that changes whether the message is opened now or later.
    flag = f" — {len(blocked)} blocked" if blocked else ""
    return f"{report.cadence.title()} report — {who} — {period}{flag}"


def _counts(report: Report) -> dict[str, int]:
    out = {key: 0 for key in COMPLETION_LABELS}
    for line in report.tasks:
        if line.completion in out:
            out[line.completion] += 1
    return out


def _rows(pairs: list[tuple[str, str]]) -> str:
    return "".join(
        f'<tr><td style="padding:2px 12px 2px 0;color:#666;">{escape(label)}</td>'
        f'<td style="padding:2px 0;"><b>{escape(value)}</b></td></tr>'
        for label, value in pairs
    )


def _issue_list(report: Report) -> str:
    """Open issues, worst first. Blocked leads — it is why this is being read."""
    order = {
        IssueSeverity.BLOCKED: 0,
        IssueSeverity.HIGH: 1,
        IssueSeverity.MEDIUM: 2,
        IssueSeverity.LOW: 3,
    }
    issues = sorted(
        (i for i in report.issues if not i.resolved),
        key=lambda i: order.get(i.severity, 4),
    )
    if not issues:
        return ""
    shown = issues[:_MAX_ISSUES]
    items = "".join(
        "<li>"
        f'<b>{escape(str(issue.severity).upper())}</b> — {escape(issue.title)}'
        + (
            f' <span style="color:#666;">(waiting on {escape(issue.waiting_on)})</span>'
            if issue.waiting_on
            else ""
        )
        + "</li>"
        for issue in shown
    )
    more = (
        f"<p style='color:#666;'>and {len(issues) - len(shown)} more.</p>"
        if len(issues) > len(shown)
        else ""
    )
    return (
        "<h3 style='margin:18px 0 6px;'>Issues</h3>"
        f"<ul style='margin:0;padding-left:18px;'>{items}</ul>{more}"
    )


def _task_list(report: Report, limit: int = _MAX_TASKS) -> str:
    if not report.tasks or limit <= 0:
        return ""
    shown = report.tasks[:limit]
    items = ""
    for line in shown:
        label = COMPLETION_LABELS.get(line.completion, line.completion)
        title = escape(line.title)
        # Linked where there is one: the reader who wants the detail should not
        # have to find it. The link carries no token — opening it uses their own
        # SharePoint access, exactly as it does everywhere else.
        if line.link:
            title = f'<a href="{escape(line.link)}">{title}</a>'
        items += (
            f'<li>{title} — <span style="color:#666;">{escape(label)}</span></li>'
        )
    more = (
        f"<p style='color:#666;'>and {len(report.tasks) - len(shown)} more.</p>"
        if len(report.tasks) > len(shown)
        else ""
    )
    return (
        "<h3 style='margin:18px 0 6px;'>Tasks</h3>"
        f"<ul style='margin:0;padding-left:18px;'>{items}</ul>{more}"
    )


#: How a health colour reads in an inbox. Text as well as colour, always — a
#: coloured dot alone is meaningless to a reader with a monochrome client, a
#: colour-blind reader, or anybody skimming on a phone in sunlight.
_RAG_INK: dict[str, str] = {
    "red": "#b42318",
    "amber": "#8a5a00",
    "green": "#1a7f47",
    "grey": "#667085",
}

_TREND_MARK: dict[str, str] = {
    "improving": "improving",
    "declining": "declining",
    "steady": "steady",
}


def _chip(rag: str | None, label: str) -> str:
    value = rag or "grey"
    ink = _RAG_INK.get(value, _RAG_INK["grey"])
    shown = "not assessed" if value == "grey" else value
    return (
        f'<td style="padding:0 14px 0 0;">'
        f'<div style="color:#666;font-size:12px;">{escape(label)}</div>'
        f'<div style="color:{ink};font-weight:700;">{escape(shown.upper())}</div>'
        f"</td>"
    )


def _dials(line: Any) -> str:
    """The five dials as a row of labelled words.

    Words rather than the coloured squares a status report uses on paper.
    Email clients strip backgrounds, block images and are read on monochrome
    screens; a report whose entire health signal was a coloured square would
    arrive as five blank cells often enough to matter.
    """
    cells = "".join(
        _chip(getattr(line, f"rag_{key}"), label)
        for key, label in (
            ("overall", "Overall"),
            ("scope", "Scope"),
            ("cost", "Costs"),
            ("schedule", "Schedule"),
            ("benefits", "Benefits"),
        )
    )
    trend = _TREND_MARK.get(line.trend_overall or "steady", "steady")
    return (
        f'<table style="border-collapse:collapse;margin:14px 0;"><tr>{cells}</tr></table>'
        f'<p style="margin:0 0 8px;color:#666;">'
        f"{line.percent_complete}% complete, overall trend {escape(trend)}.</p>"
    )


def _milestone_list(line: Any, limit: int = 6) -> str:
    """Overdue milestones, then what is next. Done ones are left out.

    A timeline does not survive an inbox, so what travels is the part somebody
    would have read off it: what has slipped, and by how long.
    """
    rows = [m for m in line.milestones if m.state != "done"]
    rows.sort(key=lambda m: (m.state != "overdue", m.due_on or date.max))
    if not rows:
        return ""

    items = ""
    for stone in rows[:limit]:
        when = stone.due_on.strftime("%d %b %Y") if stone.due_on else "no date"
        slip = ""
        if stone.baseline_due_on and stone.due_on and stone.due_on != stone.baseline_due_on:
            days = (stone.due_on - stone.baseline_due_on).days
            slip = f' <span style="color:#b42318;">({days:+d} days from plan)</span>'
        late = (
            ' <b style="color:#b42318;">OVERDUE</b>' if stone.state == "overdue" else ""
        )
        owner = (
            f' <span style="color:#666;">— {escape(stone.owner_name)}</span>'
            if stone.owner_name
            else ""
        )
        items += (
            f"<li>{escape(stone.name)} — {escape(when)}, "
            f"{stone.percent_complete}%{late}{slip}{owner}</li>"
        )
    more = (
        f"<p style='color:#666;'>and {len(rows) - limit} more.</p>"
        if len(rows) > limit
        else ""
    )
    return (
        "<h3 style='margin:18px 0 6px;'>Milestones</h3>"
        f"<ul style='margin:0;padding-left:18px;'>{items}</ul>{more}"
    )


def _portfolio_table(report: Report) -> str:
    """One row per project — worst health first, as the board shows them."""
    order = {"red": 0, "amber": 1, "grey": 2, "green": 3}
    rows = sorted(
        report.project_lines,
        key=lambda p: (order.get(p.rag_overall or "grey", 4), p.name),
    )
    if not rows:
        return ""

    cells = ""
    for line in rows:
        ink = _RAG_INK.get(line.rag_overall or "grey", _RAG_INK["grey"])
        health = "not assessed" if (line.rag_overall or "grey") == "grey" else line.rag_overall
        trouble = []
        if line.milestones_overdue:
            trouble.append(f"{line.milestones_overdue} milestone(s) overdue")
        if line.tasks_blocked:
            trouble.append(f"{line.tasks_blocked} blocked")
        if line.issues_open:
            trouble.append(f"{line.issues_open} issue(s)")
        cells += (
            "<tr>"
            f'<td style="padding:4px 12px 4px 0;">{escape(line.name)}</td>'
            f'<td style="padding:4px 12px 4px 0;color:{ink};font-weight:700;">'
            f"{escape(str(health).upper())}</td>"
            f'<td style="padding:4px 12px 4px 0;">{line.percent_complete}%</td>'
            f'<td style="padding:4px 0;color:#666;">{escape(", ".join(trouble))}</td>'
            "</tr>"
        )
    return (
        "<h3 style='margin:18px 0 6px;'>Projects</h3>"
        '<table style="border-collapse:collapse;font-size:13px;">'
        '<tr style="color:#666;">'
        '<th align="left" style="padding:0 12px 4px 0;">Project</th>'
        '<th align="left" style="padding:0 12px 4px 0;">Health</th>'
        '<th align="left" style="padding:0 12px 4px 0;">Complete</th>'
        '<th align="left" style="padding:0 0 4px;">Attention</th></tr>'
        f"{cells}</table>"
    )


def _narrative(line: Any) -> str:
    """The author's own words: key activities, then what needs deciding.

    The action block is second and styled apart, because it is the part that
    needs somebody to do something and it must not read as more prose.
    """
    out = ""
    if line.activities:
        out += (
            "<h3 style='margin:18px 0 6px;'>Key activities</h3>"
            f"<p style='margin:0;white-space:pre-line;'>{escape(line.activities)}</p>"
        )
    if line.action_required:
        out += (
            "<h3 style='margin:18px 0 6px;'>Management action required</h3>"
            "<p style='margin:0;padding:10px 12px;border-left:3px solid #b42318;"
            "background:#fdf3f2;white-space:pre-line;'>"
            f"{escape(line.action_required)}</p>"
        )
    return out


def _body(report: Report, link: str, rules: Any = None) -> str:
    """The message, in the shape that suits what the report is about.

    Three layouts over one skeleton. A team report leads with task counts; a
    project status report leads with the five dials and the milestones that
    have slipped; a portfolio report leads with the table of projects, worst
    first. They are the same message reordered — what changes is which fact
    gets the reader's first three seconds, and for each kind of report that is
    a different fact.
    """
    who = report.author.display_name if report.author else "Somebody"
    team = report.team.name if report.team else ""
    period = period_label(report.cadence, report.period_start, report.period_end)
    max_tasks = getattr(rules, "max_tasks_in_email", _MAX_TASKS)
    tasks = _task_list(report, max_tasks) if getattr(rules, "include_task_list", True) else ""
    issues = _issue_list(report) if getattr(rules, "include_issue_list", True) else ""

    overview = (
        f"<p style='margin:12px 0;'>{escape(report.overview)}</p>"
        if report.overview
        else ""
    )
    summary = (
        "<h3 style='margin:18px 0 6px;'>Summary</h3>"
        f"<p style='margin:0;'>{escape(report.summary)}</p>"
        if report.summary
        else ""
    )

    if report.scope == ReportScope.PROJECT and report.project_lines:
        line = report.project_lines[0]
        lead = (
            f"{escape(who)} filed a {escape(report.cadence)} status report on "
            f"<b>{escape(line.name)}</b>."
        )
        summary_rows = _rows(
            [
                ("Project", line.name + (f" ({line.code})" if line.code else "")),
                ("Lead", line.lead_name or "—"),
                ("Period", period),
                (
                    "Target end",
                    line.target_end_on.strftime("%d %b %Y") if line.target_end_on else "—",
                ),
                ("Tasks open", f"{line.tasks_open} of {line.tasks_total}"),
                ("Completed this period", str(line.tasks_completed_in_period)),
            ]
        )
        middle = _dials(line) + _narrative(line) + _milestone_list(line) + issues + tasks

    elif report.scope == ReportScope.PORTFOLIO:
        lead = (
            f"{escape(who)} filed a {escape(report.cadence)} portfolio report for "
            f"<b>{escape(team)}</b>."
        )
        lines = report.project_lines
        summary_rows = _rows(
            [
                ("Team", team),
                ("Period", period),
                ("Projects", str(len(lines))),
                ("Red", str(sum(1 for p in lines if p.rag_overall == "red"))),
                ("Amber", str(sum(1 for p in lines if p.rag_overall == "amber"))),
                ("Milestones overdue", str(sum(p.milestones_overdue for p in lines))),
            ]
        )
        # No task list at this altitude, and no milestone timeline: six
        # projects' milestones in one message is a message nobody finishes.
        middle = _portfolio_table(report) + issues

    else:
        counts = _counts(report)
        lead = f"{escape(who)} filed a {escape(report.cadence)} report."
        summary_rows = _rows(
            [
                ("Team", team),
                ("Period", period),
                ("Tasks", str(len(report.tasks))),
                ("Done", str(counts["done"])),
                ("In progress", str(counts["in_progress"])),
                ("Blocked", str(counts["blocked"])),
            ]
        )
        middle = issues + tasks

    return f"""\
<div style="font-family:system-ui,Segoe UI,Arial,sans-serif;font-size:14px;color:#111;">
  <p style="margin:0 0 12px;">{lead}</p>
  {overview}
  <table style="border-collapse:collapse;margin:12px 0;">{summary_rows}</table>
  {middle}
  {summary}
  <p style="margin:20px 0 0;">
    <a href="{escape(link)}"
       style="background:#111;color:#fff;padding:9px 16px;border-radius:6px;
              text-decoration:none;display:inline-block;">Open the report</a>
  </p>
</div>"""


class ReportMailer(GraphMailer):
    """Every message a report sends, from the person who filed it."""

    async def send_submitted(
        self, report: Report, recipients: list[str], *, link: str, rules: Any = None
    ) -> dict[str, Any]:
        """Mail one filed report to the people it goes to.

        Sent from the author's own mailbox, addressed by their Entra object id
        as every other notification here is. Somebody with no Entra identity has
        no mailbox to send from; the caller records that against the report
        rather than failing the submission over it.
        """
        return await self.send(
            sender=report.author.entra_object_id if report.author else "",
            recipients=recipients,
            subject=_subject(report),
            html=_body(report, link, rules),
        )
