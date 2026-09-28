"""The two emails the follow-up sends.

**The ask** goes to the person holding the task: its bid is not submitted —
because it was marked Not Submitted, or because its due time passed — please
say why. It says in as many words that if they have already updated the task
they can ignore the mail or mark it as a false positive, and gives that its own
button: the commonest reason for the mail is a status nobody moved, and a
message that reads as an accusation to somebody who submitted on time is how
people learn to filter a sender.

**The reason** goes to the team's managers and approvers, sent as the person
who gave it, so a reply reaches them rather than a robot.

Both are one card in the house colours, built from tables with inline styles —
the only layout Outlook renders faithfully. Both link to the form in the app,
which is the record; the mail is a way in.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape

from app.core.mail import GraphMailer
from app.models.followup import TaskFollowup
from app.models.user import User

#: The UAE, which keeps no daylight saving, so a fixed offset is exact.
_GULF = timezone(timedelta(hours=4), "GST")

# The costing report's palette, so the ERP's mail looks like the ERP.
_NAVY = "#0e5e80"
_INK = "#22303f"
_MUTED = "#64727f"
_LINE = "#e2e9ef"
_PANEL = "#f7fbfd"
_ALERT = "#d62d7d"
_FONT = "font-family:Segoe UI,Arial,sans-serif"


def _when(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.astimezone(_GULF).strftime("%a %d %b %Y, %H:%M") + " UAE"


def _facts(pairs: list[tuple[str, str]]) -> str:
    rows = "".join(
        f"<tr><td style='padding:6px 16px 6px 0;color:{_MUTED};font-size:13px;"
        f"white-space:nowrap;vertical-align:top'>{escape(label)}</td>"
        f"<td style='padding:6px 0;color:{_INK};font-size:13px;font-weight:600'>"
        f"{escape(value)}</td></tr>"
        for label, value in pairs
    )
    return (
        f"<table role='presentation' cellpadding='0' cellspacing='0' width='100%' "
        f"style='background:{_PANEL};border:1px solid {_LINE};border-radius:8px'>"
        f"<tr><td style='padding:10px 16px'><table role='presentation' cellpadding='0' "
        f"cellspacing='0'>{rows}</table></td></tr></table>"
    )


def _button(link: str, text: str, *, primary: bool = True) -> str:
    style = (
        f"background:{_NAVY};color:#ffffff;border:1px solid {_NAVY}"
        if primary
        else f"background:#ffffff;color:{_NAVY};border:1px solid {_NAVY}"
    )
    return (
        f"<a href='{escape(link)}' style='{style};display:inline-block;padding:11px 20px;"
        f"border-radius:6px;font-size:14px;font-weight:600;text-decoration:none;{_FONT}'>"
        f"{escape(text)}</a>"
    )


def _card(*, eyebrow: str, heading: str, accent: str, body: str, footer: str) -> str:
    """One centred card: a coloured bar, a heading, the body, a quiet footer."""
    return (
        f"<table role='presentation' cellpadding='0' cellspacing='0' width='100%' "
        f"style='background:#eef2f5;padding:24px 0;{_FONT}'><tr><td align='center'>"
        f"<table role='presentation' cellpadding='0' cellspacing='0' width='600' "
        f"style='max-width:600px;background:#ffffff;border-radius:10px;overflow:hidden;"
        f"border:1px solid {_LINE}'>"
        f"<tr><td style='height:5px;background:{accent};font-size:0;line-height:0'>&nbsp;</td></tr>"
        f"<tr><td style='padding:22px 28px 6px'>"
        f"<div style='color:{_MUTED};font-size:11px;letter-spacing:1px;text-transform:uppercase'>"
        f"{escape(eyebrow)}</div>"
        f"<div style='color:{_INK};font-size:20px;font-weight:700;margin-top:6px'>"
        f"{escape(heading)}</div></td></tr>"
        f"<tr><td style='padding:10px 28px 24px;color:{_INK};font-size:14px;line-height:1.55'>"
        f"{body}</td></tr>"
        f"<tr><td style='padding:14px 28px;border-top:1px solid {_LINE};color:{_MUTED};"
        f"font-size:11.5px;line-height:1.5'>{footer}</td></tr>"
        f"</table></td></tr></table>"
    )


def _first_name(row: TaskFollowup) -> str:
    name = row.assignee.display_name if row.assignee else ""
    return (name.split() or ["there"])[0]


# ── the ask ────────────────────────────────────────────────────────────


def ask_subject(row: TaskFollowup, *, early: bool = False) -> str:
    what = "Marked Not Submitted" if early else "Not submitted by the due time"
    return f"Reason needed: {what} — {row.task_title[:110]}"


def ask_body(row: TaskFollowup, link: str, *, early: bool = False) -> str:
    """``early``: asked before the due time, because it was marked Not Submitted."""
    facts = [("Task", row.task_title)]
    if row.end_user:
        facts.append(("End user", row.end_user))
    facts += [
        ("Bid closes" if early else "Was due", _when(row.due_at)),
        ("Submission status", row.status_at_ask or "Not set"),
    ]
    lead = (
        "This task is marked <b>Not Submitted</b> on the Proposals list."
        if early
        else "This task's due time has passed and its bid is not marked "
        "<b>Submitted</b> on the Proposals list."
    )
    sharepoint = (
        f"<br>Update it in SharePoint: <a href='{escape(row.task_url)}' "
        f"style='color:{_NAVY}'>open the task</a>."
        if row.task_url
        else ""
    )
    body = (
        f"<p style='margin:0 0 12px'>Hi {escape(_first_name(row))},</p>"
        f"<p style='margin:0 0 16px'>{lead} Please tell your manager why — a sentence "
        f"or two is enough.</p>"
        f"{_facts(facts)}"
        f"<p style='margin:22px 0 0'>{_button(link, 'Give the reason')}</p>"
        f"<table role='presentation' cellpadding='0' cellspacing='0' width='100%' "
        f"style='margin-top:22px;border-left:3px solid {_NAVY};background:{_PANEL}'>"
        f"<tr><td style='padding:12px 16px;font-size:13px;color:{_INK}'>"
        f"<b>Already submitted or updated it?</b><br>"
        f"Please ignore this mail — or mark it as a false positive so nobody follows it up."
        f"<div style='margin-top:12px'>"
        f"{_button(link + '?false-positive=1', 'Mark as false positive', primary=False)}"
        f"</div></td></tr></table>"
    )
    footer = (
        f"Sent by Hamdaz ERP because this task is assigned to you.{sharepoint}"
        f"<br>If the button does not open, copy this link: {escape(link)}"
    )
    return _card(
        eyebrow="Proposals · Reason needed",
        heading=row.task_title[:140],
        accent=_ALERT,
        body=body,
        footer=footer,
    )


# ── the reason, to the managers ────────────────────────────────────────


def reason_subject(row: TaskFollowup, who: str) -> str:
    return f"Reason given: {who} — {row.task_title[:100]}"


def reason_body(row: TaskFollowup, who: str, link: str) -> str:
    facts = [("Task", row.task_title)]
    if row.end_user:
        facts.append(("End user", row.end_user))
    facts += [
        ("Due", _when(row.due_at)),
        ("Submission status", row.status_at_ask or "Not set"),
        ("Answered", _when(row.answered_at)),
    ]
    reason = escape(row.reason or "").replace("\n", "<br>")
    body = (
        f"<p style='margin:0 0 14px'>{escape(who)} has said why this bid is not submitted.</p>"
        f"<table role='presentation' cellpadding='0' cellspacing='0' width='100%' "
        f"style='margin-bottom:16px;border-left:3px solid {_NAVY};background:{_PANEL}'>"
        f"<tr><td style='padding:14px 16px;font-size:14px;color:{_INK};line-height:1.55'>"
        f"{reason}</td></tr></table>"
        f"{_facts(facts)}"
        f"<p style='margin:22px 0 0'>{_button(link, 'Open in Hamdaz ERP')}</p>"
    )
    footer = (
        f"Sent from {escape(who)}'s mailbox by Hamdaz ERP — reply to reach them."
        f"<br>If the button does not open, copy this link: {escape(link)}"
    )
    return _card(
        eyebrow="Proposals · Reason for a late task",
        heading=row.task_title[:140],
        accent=_NAVY,
        body=body,
        footer=footer,
    )


class FollowupMailer(GraphMailer):
    async def send_ask(
        self, row: TaskFollowup, *, sender: User, link: str, early: bool = False
    ) -> None:
        await self.send(
            sender=sender.entra_object_id,
            recipients=[row.assignee_email],
            subject=ask_subject(row, early=early),
            html=ask_body(row, link, early=early),
        )

    async def send_reason(
        self, row: TaskFollowup, *, sender: User, recipients: list[str], link: str
    ) -> None:
        who = sender.display_name
        await self.send(
            sender=sender.entra_object_id,
            recipients=recipients,
            subject=reason_subject(row, who),
            html=reason_body(row, who, link),
        )
