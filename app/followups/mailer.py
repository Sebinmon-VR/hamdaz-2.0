"""The follow-up's emails: the ask, the reason, and the end-of-day report.

Written to be read in ten seconds on a phone, in Outlook, in light or dark
mode: a short heading, the facts in a plain two-column table, one or two
buttons with plain names, and a signature that says this came from the
system rather than from a colleague. Tables and inline styles only — the
one layout every mail client renders the same.

**The ask** goes to the person holding the task. It says in as many words that
if they have already updated the task they can ignore it or press "Already
Updated" — the commonest reason for the mail is a status nobody moved.

**The reason** goes to the team's managers and approvers, sent as the person
who gave it, so a reply reaches them.

The Hamdaz logo travels inside the message as an inline image, so it shows
without the reader having to allow images from a server.
"""

from __future__ import annotations

import functools
import io
from datetime import datetime, timedelta, timezone
from html import escape

from app.core.mail import Attachment, GraphMailer
from app.models.followup import TaskFollowup
from app.models.user import User

#: The UAE, which keeps no daylight saving, so a fixed offset is exact.
_GULF = timezone(timedelta(hours=4), "GST")

# The costing report's palette.
_NAVY = "#0e5e80"
_INK = "#1f2a36"
_MUTED = "#5f6b77"
_LINE = "#dfe5ea"
_HEAD = "#f1f5f8"
_ALERT = "#c0265f"
_OK = "#1f7a4d"
_FONT = "font-family:Segoe UI,Arial,sans-serif"

LOGO_CID = "hamdaz-logo"


@functools.cache
def _logo_png() -> bytes | None:
    """The logo, shrunk for a signature — a few kilobytes, not the print file."""
    from app.quoting.report_pdf import LOGO

    if not LOGO.exists():
        return None
    try:
        from PIL import Image

        image = Image.open(LOGO)
        image.thumbnail((160, 125))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()
    except Exception:  # noqa: BLE001 - no Pillow, or a bad file: send the original
        return LOGO.read_bytes()


def logo_attachment() -> list[Attachment]:
    png = _logo_png()
    return [Attachment("hamdaz-logo.png", png, "image/png", content_id=LOGO_CID)] if png else []


def _when(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.astimezone(_GULF).strftime("%d %b %Y, %H:%M") + " UAE"


# ── building blocks ────────────────────────────────────────────────────


def facts(pairs: list[tuple[str, str]]) -> str:
    """Label and value, one per row, ruled — the details of one task."""
    rows = "".join(
        f"<tr><td style='padding:8px 12px;border-bottom:1px solid {_LINE};background:{_HEAD};"
        f"color:{_MUTED};font-size:13px;width:150px;vertical-align:top'>{escape(label)}</td>"
        f"<td style='padding:8px 12px;border-bottom:1px solid {_LINE};color:{_INK};"
        f"font-size:13px;vertical-align:top'>{escape(value)}</td></tr>"
        for label, value in pairs
    )
    return (
        f"<table role='presentation' cellpadding='0' cellspacing='0' width='100%' "
        f"style='border-collapse:collapse;border:1px solid {_LINE}'>{rows}</table>"
    )


def grid(headings: list[str], rows: list[list[str]], *, colours: list[str | None] | None = None) -> str:
    """A data table: a navy header row, then one row per item."""
    head = "".join(
        f"<th align='left' style='padding:8px 10px;background:{_NAVY};color:#ffffff;"
        f"font-size:12px;font-weight:600'>{escape(h)}</th>"
        for h in headings
    )
    body = ""
    for i, row in enumerate(rows):
        shade = "#ffffff" if i % 2 == 0 else "#f8fafb"
        cells = ""
        for j, value in enumerate(row):
            colour = (colours[j] if colours and j < len(colours) else None) or _INK
            cells += (
                f"<td style='padding:8px 10px;border-bottom:1px solid {_LINE};background:{shade};"
                f"color:{colour};font-size:12.5px;vertical-align:top'>{escape(value)}</td>"
            )
        body += f"<tr>{cells}</tr>"
    return (
        f"<table role='presentation' cellpadding='0' cellspacing='0' width='100%' "
        f"style='border-collapse:collapse;border:1px solid {_LINE}'>"
        f"<tr>{head}</tr>{body}</table>"
    )


def counts(pairs: list[tuple[str, int, bool]]) -> str:
    """The headline numbers in one row. The flag colours a non-zero figure."""
    cells = "".join(
        f"<td align='center' style='padding:12px 6px;border:1px solid {_LINE};background:{_HEAD}'>"
        f"<div style='font-size:22px;font-weight:700;color:{_ALERT if alert and n else _INK}'>{n}</div>"
        f"<div style='font-size:12px;color:{_MUTED};margin-top:2px'>{escape(label)}</div></td>"
        for label, n, alert in pairs
    )
    return (
        f"<table role='presentation' cellpadding='0' cellspacing='0' width='100%' "
        f"style='border-collapse:collapse'><tr>{cells}</tr></table>"
    )


def button(link: str, text: str, *, primary: bool = True) -> str:
    style = (
        f"background:{_NAVY};color:#ffffff;border:1px solid {_NAVY}"
        if primary
        else f"background:#ffffff;color:{_NAVY};border:1px solid {_NAVY}"
    )
    return (
        f"<a href='{escape(link)}' style='{style};display:inline-block;padding:10px 22px;"
        f"border-radius:4px;font-size:14px;font-weight:600;text-decoration:none;{_FONT}'>"
        f"{escape(text)}</a>"
    )


def heading(text: str, *, eyebrow: str) -> str:
    return (
        f"<div style='color:{_MUTED};font-size:11px;letter-spacing:1px;text-transform:uppercase'>"
        f"{escape(eyebrow)}</div>"
        f"<div style='color:{_INK};font-size:19px;font-weight:700;margin:4px 0 14px'>"
        f"{escape(text)}</div>"
    )


def signature() -> str:
    """The system's own signature: this was written by software, not a person."""
    logo = (
        f"<td style='padding-right:14px;vertical-align:middle'>"
        f"<img src='cid:{LOGO_CID}' width='64' alt='Hamdaz' style='display:block;border:0'></td>"
        if _logo_png()
        else ""
    )
    return (
        f"<table role='presentation' cellpadding='0' cellspacing='0' "
        f"style='margin-top:26px;border-top:2px solid {_NAVY};padding-top:12px'><tr>"
        f"{logo}<td style='vertical-align:middle;{_FONT}'>"
        f"<div style='font-size:13px;font-weight:700;color:{_NAVY}'>Hamdaz ERP — Automated Report</div>"
        f"<div style='font-size:12px;color:{_MUTED};margin-top:2px'>AI-generated by the Hamdaz ERP system. "
        f"Please do not reply to the automated parts of this message.</div>"
        f"<div style='font-size:12px;color:{_INK};margin-top:4px'><b>Hamdaz Technologies</b> · "
        f"Hamdaztech Technology Services L.L.C · Abu Dhabi, UAE</div>"
        f"<div style='font-size:12px;margin-top:2px'><a href='https://www.hamdaz.com' "
        f"style='color:{_NAVY};text-decoration:none'>www.hamdaz.com</a> · hello@hamdaz.com · "
        f"+971 2 626 5780</div></td></tr></table>"
    )


def page(body: str) -> str:
    """The whole message: white, left-aligned, 640 wide, then the signature."""
    return (
        f"<div style='background:#ffffff;{_FONT};color:{_INK}'>"
        f"<table role='presentation' cellpadding='0' cellspacing='0' width='100%' "
        f"style='max-width:640px'><tr><td style='padding:8px 4px;{_FONT};color:{_INK};"
        f"font-size:14px;line-height:1.5'>{body}{signature()}</td></tr></table></div>"
    )


# ── the ask ────────────────────────────────────────────────────────────


def ask_subject(row: TaskFollowup, *, early: bool = False) -> str:
    what = "Not Submitted" if early else "Past Due, Not Submitted"
    return f"Reason Required: {row.task_title[:100]} ({what})"


def ask_body(row: TaskFollowup, link: str, *, early: bool = False) -> str:
    """``early``: asked before the due time, because it was marked Not Submitted."""
    first = ((row.assignee.display_name if row.assignee else "").split() or ["there"])[0]
    lead = (
        "This task is marked <b>Not Submitted</b> on the Proposals list."
        if early
        else "This task is past its due time and is not marked <b>Submitted</b> on the "
        "Proposals list."
    )
    details = [("Task", row.task_title)]
    if row.end_user:
        details.append(("End user", row.end_user))
    details += [
        ("Bid closing" if early else "Was due", _when(row.due_at)),
        ("Submission status", row.status_at_ask or "Not set"),
    ]
    body = (
        heading("Reason required", eyebrow="Proposals")
        + f"<p style='margin:0 0 14px'>Hi {escape(first)},<br>{lead} Please give the reason — "
        f"a sentence or two is enough.</p>"
        + facts(details)
        + f"<p style='margin:20px 0 6px'>{button(link, 'Submit Reason')}&nbsp;&nbsp;"
        f"{button(link + '?false-positive=1', 'Already Updated', primary=False)}</p>"
        + f"<p style='margin:10px 0 0;font-size:12.5px;color:{_MUTED}'>If you have already submitted "
        f"the bid or updated its status, you can ignore this email, or press "
        f"<b>Already Updated</b> so it is not followed up.</p>"
        + (
            f"<p style='margin:6px 0 0;font-size:12.5px;color:{_MUTED}'>Task in SharePoint: "
            f"<a href='{escape(row.task_url)}' style='color:{_NAVY}'>open</a></p>"
            if row.task_url
            else ""
        )
    )
    return page(body)


# ── the reason, to the managers ────────────────────────────────────────


def reason_subject(row: TaskFollowup, who: str) -> str:
    return f"Reason Submitted: {who} — {row.task_title[:100]}"


def reason_body(row: TaskFollowup, who: str, link: str) -> str:
    details = [("Submitted by", who), ("Task", row.task_title)]
    if row.end_user:
        details.append(("End user", row.end_user))
    details += [
        ("Due", _when(row.due_at)),
        ("Submission status", row.status_at_ask or "Not set"),
        ("Reason", row.reason or ""),
    ]
    body = (
        heading("Reason submitted", eyebrow="Proposals")
        + f"<p style='margin:0 0 14px'>{escape(who)} has given the reason this bid is not "
        f"submitted.</p>"
        + facts(details)
        + f"<p style='margin:20px 0 0'>{button(link, 'View Details')}</p>"
    )
    return page(body)


class FollowupMailer(GraphMailer):
    async def send_ask(
        self, row: TaskFollowup, *, sender: User, link: str, early: bool = False
    ) -> None:
        await self.send(
            sender=sender.entra_object_id,
            recipients=[row.assignee_email],
            subject=ask_subject(row, early=early),
            html=ask_body(row, link, early=early),
            attachments=logo_attachment(),
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
            attachments=logo_attachment(),
        )
