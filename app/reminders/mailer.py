"""The status reminder's mail, in the follow-up's house style.

Built from the follow-up mail's parts, and sent through its ``_deliver`` —
so the testing address catches these the same way, sent from and to it.
"""

from __future__ import annotations

from html import escape

from app.followups.mailer import (
    _MUTED,
    _NAVY,
    FollowupMailer,
    _cap,
    _when,
    button,
    facts,
    heading,
    page,
)
from app.models.status_reminder import StatusReminder
from app.models.user import User


def reminder_subject(rows: list[StatusReminder]) -> str:
    n = len(rows)
    if n == 1:
        return f"Status Update Needed: {rows[0].task_title[:100]}"
    return f"Status Update Needed: {n} tasks due soon"


def _block(row: StatusReminder, link: str) -> str:
    """One task: what it is, when it is due, what the list says, its button."""
    details = [("Task", row.task_title)]
    if row.end_user:
        details.append(("End user", row.end_user))
    details += [
        ("Due", _when(row.due_at)),
        ("Status", row.status_at_ask or "Not set"),
        ("Submission status", row.submission_at_ask or "Not set"),
        ("Remarks", _cap(row.remarks_at_ask or "") or "Not written yet"),
        ("Working notes", _cap(row.working_notes_at_ask or "") or "Not written yet"),
    ]
    open_task = (
        f"&nbsp;&nbsp;<a href='{escape(row.task_url)}' style='color:{_NAVY};font-size:12.5px'>"
        f"Open in SharePoint</a>"
        if row.task_url
        else ""
    )
    return (
        f"<div style='margin:0 0 18px'>{facts(details)}"
        f"<p style='margin:10px 0 0'>{button(link, 'Update Status')}{open_task}</p></div>"
    )


def reminder_body(rows: list[StatusReminder], links: dict) -> str:
    first = ((rows[0].assignee.display_name if rows[0].assignee else "").split() or ["there"])[0]
    n = len(rows)
    body = (
        heading("Status update needed", eyebrow="Proposals")
        + f"<p style='margin:0 0 14px'>Hi {escape(first)},<br>"
        f"{'This task is' if n == 1 else f'These {n} tasks are'} due soon and not yet marked "
        f"<b>Completed</b> or <b>Submitted</b> on the Proposals list. Please check "
        f"{'its' if n == 1 else 'each one’s'} status, submission status, remarks and working "
        f"notes, and update them — press <b>Update Status</b>.</p>"
        + "".join(_block(r, links[r.id]) for r in rows)
        + f"<p style='margin:6px 0 0;font-size:12.5px;color:{_MUTED}'>If everything is already "
        f"right, open the form and press <b>Confirm</b> without changing anything.</p>"
    )
    return page(body)


#: The four columns as people know them, in the order the form shows them.
_LABELS = (
    ("Status", "Status"),
    ("SubmissionStatus", "Submission status"),
    ("Remarks", "Remarks"),
    ("WorkingNotes", "Working notes"),
)


def update_subject(row: StatusReminder, who: str) -> str:
    return f"Status Updated: {who} — {row.task_title[:100]}"


def update_body(
    row: StatusReminder, who: str, before: dict[str, str], link: str, *, writes: bool
) -> str:
    """One answer, for the managers: each column before and after, and
    whether it reached the Proposals list."""
    changes = row.changes or {}
    lines = []
    for column, label in _LABELS:
        old = (before.get(column) or "").strip()
        if column in changes:
            new = (changes[column] or "").strip()
            lines.append((label, f"{_cap(old) or 'blank'}  →  {_cap(new) or 'blank'}"))
        else:
            lines.append((label, f"{_cap(old) or 'blank'}  (unchanged)"))
    if not changes:
        outcome = "Confirmed as it was — nothing changed."
    elif row.written_at:
        outcome = "Written to the task on the Proposals list."
    elif row.write_error:
        outcome = f"Not written: SharePoint refused it — {row.write_error}"
    elif not writes:
        outcome = "Kept in the app only: writing to the Proposals list is switched off."
    else:
        outcome = "Kept in the app only."
    details = [("Task", row.task_title)]
    if row.end_user:
        details.append(("End user", row.end_user))
    details += [("Due", _when(row.due_at)), *lines, ("On SharePoint", outcome)]
    body = (
        heading("Status updated", eyebrow="Proposals")
        + f"<p style='margin:0 0 14px'>{escape(who)} has updated the status of a task due soon.</p>"
        + facts(details)
        + f"<p style='margin:20px 0 0'>{button(link, 'View Update')}</p>"
    )
    return page(body)


class ReminderMailer(FollowupMailer):
    async def send_update(
        self,
        row: StatusReminder,
        *,
        sender: User,
        recipients: list[str],
        before: dict[str, str],
        link: str,
        writes: bool,
        redirect_to: str | None = None,
    ) -> None:
        who = sender.display_name
        await self._deliver(
            sender=sender.entra_object_id,
            sender_name=sender.display_name,
            recipients=recipients,
            subject=update_subject(row, who),
            html=update_body(row, who, before, link, writes=writes),
            redirect_to=redirect_to,
        )

    async def send_reminders(
        self,
        rows: list[StatusReminder],
        *,
        sender: User,
        links: dict,
        redirect_to: str | None = None,
    ) -> None:
        await self._deliver(
            sender=sender.entra_object_id,
            sender_name=sender.display_name,
            recipients=[rows[0].assignee_email],
            subject=reminder_subject(rows),
            html=reminder_body(rows, links),
            redirect_to=redirect_to,
        )
