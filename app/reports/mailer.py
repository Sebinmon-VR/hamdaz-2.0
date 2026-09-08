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

from html import escape
from typing import Any

from app.core.mail import GraphMailer
from app.models.report import IssueSeverity, Report
from app.reports.catalogue import COMPLETION_LABELS, period_label

#: Beyond this the message stops being a summary. A report with forty tasks is
#: a report to open, not one to read in an inbox.
_MAX_TASKS: int = 8
_MAX_ISSUES: int = 6


def _subject(report: Report) -> str:
    who = report.author.display_name if report.author else "Somebody"
    period = period_label(report.cadence, report.period_start, report.period_end)
    blocked = [i for i in report.issues if not i.resolved and i.severity == IssueSeverity.BLOCKED]
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


def _body(report: Report, link: str, rules: Any = None) -> str:
    who = report.author.display_name if report.author else "Somebody"
    team = report.team.name if report.team else ""
    period = period_label(report.cadence, report.period_start, report.period_end)
    counts = _counts(report)
    max_tasks = getattr(rules, "max_tasks_in_email", _MAX_TASKS)
    tasks = _task_list(report, max_tasks) if getattr(rules, "include_task_list", True) else ""
    issues = _issue_list(report) if getattr(rules, "include_issue_list", True) else ""

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

    return f"""\
<div style="font-family:system-ui,Segoe UI,Arial,sans-serif;font-size:14px;color:#111;">
  <p style="margin:0 0 12px;">{escape(who)} filed a {escape(report.cadence)} report.</p>
  {overview}
  <table style="border-collapse:collapse;margin:12px 0;">{summary_rows}</table>
  {issues}
  {tasks}
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
