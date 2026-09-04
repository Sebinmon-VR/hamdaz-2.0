"""Sending the leave request to HR, from the requester's own mailbox.

Graph's ``/users/{id}/sendMail`` with an application token sends *as* that
person, so the mail lands in HR's inbox from the colleague who asked for the
leave — which is what makes it repliable.

Two safeguards, because this reaches real people:

* it is **off by default** (``LeaveSettings.notify_hr_by_email``). A development
  build must not mail colleagues while someone is clicking around a sandbox.
* a failure to send never fails the request. The leave is already recorded and
  decided; the email is a notification, not the transaction. The error is stored
  on the row so nobody has to guess whether it went.
"""

from __future__ import annotations

from typing import Any

from app.core.mail import GraphMailer, MailError
from app.models.leave import LeaveRequest

__all__ = ["LeaveMailer", "MailError"]


def _body(request: LeaveRequest) -> str:
    who = request.user.display_name
    days = request.days
    lines = [
        f"<p>{who} has requested leave.</p>",
        "<table cellpadding='6' style='border-collapse:collapse'>",
        f"<tr><td><b>Type</b></td><td>{request.leave_type}</td></tr>",
        f"<tr><td><b>From</b></td><td>{request.start_date:%A %d %B %Y}</td></tr>",
        f"<tr><td><b>To</b></td><td>{request.end_date:%A %d %B %Y}</td></tr>",
        f"<tr><td><b>Days</b></td><td>{days}</td></tr>",
    ]
    if request.reason:
        lines.append(f"<tr><td><b>Reason</b></td><td>{request.reason}</td></tr>")
    lines.append(f"<tr><td><b>Status</b></td><td>{request.status}</td></tr>")
    if request.decision_note:
        lines.append(f"<tr><td><b>Decision</b></td><td>{request.decision_note}</td></tr>")
    lines.append("</table>")

    if request.status == "rejected":
        lines.append(
            "<p>This was declined automatically by the leave rules. "
            "HR can override it if the situation is urgent.</p>"
        )
    return "".join(lines)


def _subject(request: LeaveRequest) -> str:
    span = (
        f"{request.start_date:%d %b}"
        if request.start_date == request.end_date
        else f"{request.start_date:%d %b} – {request.end_date:%d %b}"
    )
    return f"Leave request — {request.user.display_name} — {span} ({request.status})"


class LeaveMailer(GraphMailer):
    async def send_request(
        self, request: LeaveRequest, recipients: list[str]
    ) -> dict[str, Any]:
        """Mail HR as the requester, so the message is repliable."""
        if not recipients:
            raise MailError("There is nobody in the HR team to notify")
        return await self.send(
            sender=request.user.entra_object_id,
            recipients=recipients,
            subject=_subject(request),
            html=_body(request),
        )
