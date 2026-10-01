"""The BCD check's two mails, in the follow-up's house style.

Times are shown in the UAE, as BCDs are. Sent with the team lead copied; a
test (or the testing address) goes to one address only and says in a banner
who it would have reached, copies included.
"""

from __future__ import annotations

from html import escape

from app.bcd.service import edit_link
from app.followups.mailer import (
    _MUTED,
    FollowupMailer,
    _when,
    button,
    facts,
    heading,
    logo_attachment,
    page,
    test_banner,
)
from app.models.bcd_check import BcdCheck
from app.models.user import User


def _block(row: BcdCheck, link: str, *, with_person: bool = False) -> str:
    details = [("Task", row.task_title)]
    if with_person:
        details.append(("Assigned to", row.assignee.display_name if row.assignee else row.assignee_email))
    details += [
        ("Assigned", _when(row.task_created_at)),
        ("BCD on the list", "The assignment time — not the real closing date yet"),
    ]
    fix = edit_link(row.task_url)
    buttons = (
        (button(fix, "Correct the BCD in SharePoint") + "&nbsp;&nbsp;" if fix else "")
        + button(f"{link}?confirm=1", "The BCD Is Correct", primary=not fix)
    )
    return f"<div style='margin:0 0 18px'>{facts(details)}<p style='margin:10px 0 0'>{buttons}</p></div>"


def ask_subject(rows: list[BcdCheck]) -> str:
    if len(rows) == 1:
        return f"Confirm the BCD: {rows[0].task_title[:100]}"
    return f"Confirm the BCD: {len(rows)} new tasks"


def ask_body(rows: list[BcdCheck], links: dict) -> str:
    first = ((rows[0].assignee.display_name if rows[0].assignee else "").split() or ["there"])[0]
    one = len(rows) == 1
    body = (
        heading("Confirm the bid closing date", eyebrow="Proposals")
        + f"<p style='margin:0 0 14px'>Hi {escape(first)},<br>"
        f"{'This task was' if one else 'These tasks were'} assigned with the BCD set to the time of "
        f"assignment, not the real closing date. Please read the closing date from Ariba and set "
        f"the <b>BCD</b> on the task in SharePoint. If the date already there is right, press "
        f"<b>The BCD Is Correct</b>.</p>"
        + "".join(_block(r, links[r.id]) for r in rows)
        + f"<p style='margin:6px 0 0;font-size:12.5px;color:{_MUTED}'>Until the BCD is set, the "
        f"reminders and follow-ups wait for it. Not set within two working hours, this goes to "
        f"the managers.</p>"
    )
    return page(body)


def escalation_subject(rows: list[BcdCheck]) -> str:
    if len(rows) == 1:
        return f"BCD Not Confirmed: {rows[0].task_title[:100]}"
    return f"BCD Not Confirmed: {len(rows)} tasks"


def escalation_body(rows: list[BcdCheck], links: dict) -> str:
    body = (
        heading("BCD not confirmed", eyebrow="Proposals")
        + "<p style='margin:0 0 14px'>"
        + ("This task was" if len(rows) == 1 else "These tasks were")
        + " assigned with the BCD set to the time of assignment, and nobody has corrected or "
        "confirmed it within two working hours of being asked. Please set the real closing date "
        "on the task in SharePoint, or confirm the date as it stands.</p>"
        + "".join(_block(r, links[r.id], with_person=True) for r in rows)
    )
    return page(body)


class BcdMailer(FollowupMailer):
    async def _deliver_cc(
        self,
        *,
        sender: str,
        sender_name: str | None,
        to: list[str],
        cc: list[str],
        subject: str,
        html: str,
        redirect_to: str | None,
    ) -> None:
        if redirect_to:
            cell = "line-height:1.5'>"
            meant = to + [f"cc {address}" for address in cc]
            html = html.replace(cell, cell + test_banner(meant, sender_name), 1)
            # From and to the testing address only — nobody copied.
            sender, to, cc, subject = redirect_to, [redirect_to], [], f"[TEST] {subject}"
        await self.send(
            sender=sender, recipients=to, cc=cc, subject=subject, html=html,
            attachments=logo_attachment(),
        )

    async def send_ask(
        self,
        rows: list[BcdCheck],
        *,
        sender: User,
        to: list[str],
        cc: list[str],
        links: dict,
        redirect_to: str | None = None,
    ) -> None:
        await self._deliver_cc(
            sender=sender.entra_object_id or sender.email,
            sender_name=sender.display_name,
            to=to, cc=cc,
            subject=ask_subject(rows),
            html=ask_body(rows, links),
            redirect_to=redirect_to,
        )

    async def send_escalation(
        self,
        rows: list[BcdCheck],
        *,
        sender_email: str,
        to: list[str],
        cc: list[str],
        links: dict,
        redirect_to: str | None = None,
    ) -> None:
        await self._deliver_cc(
            sender=sender_email,
            sender_name=sender_email,
            to=to, cc=cc,
            subject=escalation_subject(rows),
            html=escalation_body(rows, links),
            redirect_to=redirect_to,
        )
