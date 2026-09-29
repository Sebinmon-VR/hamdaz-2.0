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


def status_not_set(row: TaskFollowup) -> bool:
    """Nobody filled in the Submission Status — a different ask from "Not
    Submitted": first update it, and only if the bid was missed, say why."""
    return not (row.status_at_ask or "").strip()


def ask_subject(row: TaskFollowup, *, early: bool = False) -> str:
    if status_not_set(row) and not early:
        return f"Action Required: Submission Status Not Set — {row.task_title[:100]}"
    what = "Not Submitted" if early else "Past Due, Not Submitted"
    return f"Reason Required: {row.task_title[:100]} ({what})"


def ask_body(row: TaskFollowup, link: str, *, early: bool = False) -> str:
    """``early``: asked before the due time, because it was marked Not Submitted."""
    first = ((row.assignee.display_name if row.assignee else "").split() or ["there"])[0]
    if status_not_set(row) and not early:
        return _ask_status_not_set(row, link, first)
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


def _ask_status_not_set(row: TaskFollowup, link: str, first: str) -> str:
    """The bid's closing time passed with no Submission Status at all.

    Two things are asked, in this order: put the status on the task — which
    is usually all that is missing — and, if the bid really was missed, give
    the reason. "Already Updated" closes it for the first case.
    """
    details = [("Task", row.task_title)]
    if row.end_user:
        details.append(("End user", row.end_user))
    details += [("Bid closing", _when(row.due_at)), ("Submission status", "Not set")]
    open_task = (
        f"<a href='{escape(row.task_url)}' style='color:{_NAVY}'>open the task in SharePoint</a>"
        if row.task_url
        else "open the task in SharePoint"
    )
    body = (
        heading("Submission status not set", eyebrow="Proposals")
        + f"<p style='margin:0 0 14px'>Hi {escape(first)},<br>The bid closing time for this task "
        f"has passed, and no <b>Submission Status</b> is set on the Proposals list.</p>"
        + facts(details)
        + f"<p style='margin:18px 0 6px;font-weight:600'>Please do one of the following:</p>"
        + f"<table role='presentation' cellpadding='0' cellspacing='0' width='100%' "
        f"style='border-collapse:collapse;border:1px solid {_LINE}'>"
        f"<tr><td style='padding:10px 12px;border-bottom:1px solid {_LINE};font-size:13px'>"
        f"<b>1. The bid was submitted</b> — {open_task}, set the Submission Status to "
        f"<b>Submitted</b>, then press <b>Already Updated</b>.</td></tr>"
        f"<tr><td style='padding:10px 12px;font-size:13px'>"
        f"<b>2. The bid was missed</b> — set the Submission Status to <b>Not Submitted</b> and "
        f"press <b>Submit Reason</b> to say why.</td></tr></table>"
        + f"<p style='margin:20px 0 6px'>{button(link, 'Submit Reason')}&nbsp;&nbsp;"
        f"{button(link + '?false-positive=1', 'Already Updated', primary=False)}</p>"
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


# ── the daily batch, to the person ─────────────────────────────────────


def test_banner(meant_for: list[str], sent_by: str | None = None) -> str:
    """Said at the top of a mail sent to the testing address instead."""
    by = f", from {escape(sent_by)}'s mailbox" if sent_by else ""
    return (
        f"<div style='margin:0 0 16px;padding:10px 12px;background:#fff4d6;"
        f"border:1px solid #f0d58a;"
        f"font-size:12.5px;color:{_INK}'><b>TEST</b> — this email would have gone to "
        f"{escape(', '.join(meant_for) or 'nobody')}{by}.</div>"
    )


def _task_block(row: TaskFollowup, link: str) -> str:
    """One task in a person's list: what it is, when it was due, and its buttons."""
    details = [("Task", row.task_title)]
    if row.end_user:
        details.append(("End user", row.end_user))
    details += [
        ("Was due", _when(row.due_at)),
        ("Submission status", row.status_at_ask or "Not set"),
    ]
    hint = (
        f"<p style='margin:8px 0 0;font-size:12.5px;color:{_MUTED}'>No Submission Status is set. "
        f"If the bid went in, set it to <b>Submitted</b> and press <b>Already Updated</b>; if it "
        f"was missed, press <b>Submit Reason</b>.</p>"
        if status_not_set(row)
        else ""
    )
    open_task = (
        f"&nbsp;&nbsp;<a href='{escape(row.task_url)}' style='color:{_NAVY};font-size:12.5px'>"
        f"Open in SharePoint</a>"
        if row.task_url
        else ""
    )
    return (
        f"<div style='margin:0 0 16px'>{facts(details)}{hint}"
        f"<p style='margin:10px 0 0'>{button(link, 'Submit Reason')}&nbsp;&nbsp;"
        f"{button(link + '?false-positive=1', 'Already Updated', primary=False)}"
        f"{open_task}</p></div>"
    )


def batch_subject(rows: list[TaskFollowup]) -> str:
    n = len(rows)
    return f"Reason Required: {n} task{'s' if n != 1 else ''} past due, not submitted"


def batch_body(rows: list[TaskFollowup], links: dict, *, ask_time: str) -> str:
    """Today's tasks first; then the ones carried over from yesterday, apart,
    with a note saying why they are in today's list."""
    first = ((rows[0].assignee.display_name if rows[0].assignee else "").split() or ["there"])[0]
    today = [r for r in rows if not r.carried_over]
    carried = [r for r in rows if r.carried_over]
    body = (
        heading("Reasons required", eyebrow="Proposals")
        + f"<p style='margin:0 0 14px'>Hi {escape(first)},<br>These tasks are past their due "
        f"time and not marked <b>Submitted</b> on the Proposals list. Please give the reason "
        f"for each — a sentence or two is enough. If you have already submitted a bid or "
        f"updated its status, press <b>Already Updated</b> on it.</p>"
    )
    if today:
        body += (
            f"<p style='margin:18px 0 8px;font-weight:700;color:{_NAVY}'>"
            f"Due today ({len(today)})</p>"
            + "".join(_task_block(r, links[r.id]) for r in today)
        )
    if carried:
        body += (
            f"<p style='margin:22px 0 4px;font-weight:700;color:{_ALERT}'>"
            f"Carried over from yesterday ({len(carried)})</p>"
            f"<p style='margin:0 0 10px;font-size:12.5px;color:{_MUTED}'>These were due after "
            f"yesterday's ask time ({escape(ask_time)}), so they are asked today, in their own "
            f"list.</p>"
            + "".join(_task_block(r, links[r.id]) for r in carried)
        )
    return page(body)


# ── one person's day, to the managers, at the closing time ───────────


def _person_state(row: TaskFollowup) -> tuple[str, str | None]:
    """How a task reads in the person's report, and its colour."""
    if row.status == "answered":
        return "Reason given", None
    if row.status == "false_positive":
        return "Already updated", _OK
    if row.status == "resolved":
        return "Closed", None
    return "Not answered", _ALERT


def person_report_subject(who: str, day) -> str:
    return f"Reasons: {who} — {day:%d %b %Y}"


def person_report_body(who: str, rows: list[TaskFollowup], day, link: str) -> str:
    """Everything one person was asked about that day, answered or not."""
    states = [_person_state(r) for r in rows]
    missing = sum(1 for label, _ in states if label == "Not answered")
    answered = sum(1 for label, _ in states if label == "Reason given")
    updated = sum(1 for label, _ in states if label == "Already updated")
    table = grid(
        ["Task", "Due", "Status", "Reason / note"],
        [
            [
                r.task_title + (" (carried over from the previous day)" if r.carried_over else ""),
                _when(r.due_at),
                label,
                (r.reason or r.resolved_note or "").strip()
                or ("No reason given by the closing time." if label == "Not answered" else ""),
            ]
            for r, (label, _) in zip(rows, states, strict=True)
        ],
        colours=[None, None, None, None],
    )
    # The grid colours by column; a missing reason is said in words as well.
    body = (
        heading(f"Reasons from {who}", eyebrow=f"Proposals · {day:%d %b %Y}")
        + counts(
            [
                ("Asked", len(rows), False),
                ("Reason given", answered, False),
                ("Already updated", updated, False),
                ("Not answered", missing, True),
            ]
        )
        + "<div style='height:14px'></div>"
        + table
        + f"<p style='margin:20px 0 0'>{button(link, 'View Follow-ups')}</p>"
    )
    return page(body)


class FollowupMailer(GraphMailer):
    """Every follow-up mail goes through :meth:`_deliver`, so the testing
    address (``FollowupSettings.test_mail_to``) catches all of them alike."""

    async def _deliver(
        self,
        *,
        sender: str,
        recipients: list[str],
        subject: str,
        html: str,
        redirect_to: str | None,
        sender_name: str | None = None,
    ) -> None:
        if redirect_to:
            # Into the page's own cell, above the heading: the first thing read.
            cell = "line-height:1.5'>"
            html = html.replace(cell, cell + test_banner(recipients, sender_name), 1)
            # Sent *as* the testing address too, not as the colleague it names:
            # a test must not leave mail in somebody else's Sent Items.
            sender, recipients, subject = redirect_to, [redirect_to], f"[TEST] {subject}"
        await self.send(
            sender=sender,
            recipients=recipients,
            subject=subject,
            html=html,
            attachments=logo_attachment(),
        )

    async def send_ask(
        self,
        row: TaskFollowup,
        *,
        sender: User,
        link: str,
        early: bool = False,
        redirect_to: str | None = None,
    ) -> None:
        await self._deliver(
            sender=sender.entra_object_id,
            sender_name=sender.display_name,
            recipients=[row.assignee_email],
            subject=ask_subject(row, early=early),
            html=ask_body(row, link, early=early),
            redirect_to=redirect_to,
        )

    async def send_batch(
        self,
        rows: list[TaskFollowup],
        *,
        sender: User,
        links: dict,
        ask_time: str,
        redirect_to: str | None = None,
    ) -> None:
        await self._deliver(
            sender=sender.entra_object_id,
            sender_name=sender.display_name,
            recipients=[rows[0].assignee_email],
            subject=batch_subject(rows),
            html=batch_body(rows, links, ask_time=ask_time),
            redirect_to=redirect_to,
        )

    async def send_reason(
        self,
        row: TaskFollowup,
        *,
        sender: User,
        recipients: list[str],
        link: str,
        redirect_to: str | None = None,
    ) -> None:
        who = sender.display_name
        await self._deliver(
            sender=sender.entra_object_id,
            sender_name=sender.display_name,
            recipients=recipients,
            subject=reason_subject(row, who),
            html=reason_body(row, who, link),
            redirect_to=redirect_to,
        )

    async def send_person_report(
        self,
        rows: list[TaskFollowup],
        *,
        person: User,
        sender_email: str,
        recipients: list[str],
        day,
        link: str,
        redirect_to: str | None = None,
    ) -> None:
        who = person.display_name
        await self._deliver(
            sender=sender_email,
            sender_name=sender_email,
            recipients=recipients,
            subject=person_report_subject(who, day),
            html=person_report_body(who, rows, day, link),
            redirect_to=redirect_to,
        )
