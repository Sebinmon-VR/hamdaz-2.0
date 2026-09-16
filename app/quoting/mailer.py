"""Telling the approvers a quote is waiting for them.

Sent as the person who raised it, from their own mailbox, so it arrives from a
colleague and a reply reaches them rather than a no-reply address.

**The link is the point of the message.** An approver reading it on a phone
should be one tap from the quote, not hunting for it in a list. The address it
points at is ``frontend_url``, so this follows the deployment rather than
needing a second thing to change.

A failure to send never fails the submission. The quote is already with the
approvers as far as the system is concerned; the email is a notification, not
the transaction. What went wrong is stored on the row so nobody has to guess
whether it went.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Final

from app.core.mail import Attachment, GraphMailer
from app.models.quoting import QuoteComment, QuoteRequest, QuoteReview
from app.quoting import workbook
from app.quoting.workbook import XLSX_TYPE


def _money(value: Decimal | None, currency: str) -> str:
    return f"{currency} {value:,.2f}" if value is not None else "—"


def _supplier_name(request: QuoteRequest) -> str | None:
    """The chosen supplier, out of the analysis already attached to the quote."""
    chosen = request.selected_supplier_quote_id
    if chosen is None or request.comparison is None:
        return None
    for row in (request.comparison.analysis or {}).get("suppliers", []):
        if str(row.get("quote_id")) == str(chosen):
            return row.get("supplier_name")
    return None


def _subject(request: QuoteRequest) -> str:
    what = request.reference or request.title
    return f"Quote approval needed — {what} — {request.customer_name}"


def _body(request: QuoteRequest, link: str) -> str:
    who = request.created_by.display_name if request.created_by else "Somebody"
    rows = [
        ("Customer", request.customer_name),
        ("Title", request.title),
        ("Total", _money(request.total, request.currency)),
        ("Lines", str(len(request.items))),
    ]
    supplier = _supplier_name(request)
    if supplier:
        rows.append(("Priced from", supplier))
    if request.win_probability is not None:
        # The probability travels with the count it came from, here as
        # everywhere else. On its own it invites more confidence than it earned.
        basis = (request.win_basis or {}).get("decided")
        seen = f" (from {basis} decided quotes)" if basis else ""
        rows.append(("Win probability", f"{request.win_probability:.0%}{seen}"))
    if request.cf_bcd:
        rows.append(("Bid closing", f"{request.cf_bcd:%A %d %B %Y}"))
    if request.revision > 1:
        rows.append(("Revision", str(request.revision)))

    cells = "".join(
        f"<tr><td style='padding:4px 12px 4px 0'><b>{label}</b></td>"
        f"<td style='padding:4px 0'>{value}</td></tr>"
        for label, value in rows
    )
    return (
        f"<p>{who} has sent a quote for your approval.</p>"
        f"<table cellpadding='0' style='border-collapse:collapse'>{cells}</table>"
        f"<p style='margin-top:18px'>"
        f"<a href='{link}' style='background:#1a1a1a;color:#fff;padding:10px 18px;"
        f"border-radius:6px;text-decoration:none'>Open the quote</a></p>"
        f"<p style='color:#666;font-size:12px'>{link}</p>"
    )


#: How a decision reads in a subject line. "rework" is what the code calls it;
#: "changes requested" is what happened.
_DECISIONS = {
    "approve": "approved",
    "reject": "rejected",
    "rework": "sent back for changes",
    "negotiate": "reopened for negotiation",
}


def _what(review: QuoteReview) -> str:
    return _DECISIONS.get(str(review.action), str(review.action))


def _note_block(note: str | None, heading: str) -> str:
    if not (note or "").strip():
        return ""
    return (
        f"<p style='margin-top:16px'><b>{heading}</b></p>"
        f"<blockquote style='margin:8px 0;padding:8px 14px;border-left:3px solid #ddd;"
        f"color:#333'>{note}</blockquote>"
    )


def _link_block(link: str, label: str) -> str:
    return (
        f"<p style='margin-top:18px'>"
        f"<a href='{link}' style='background:#1a1a1a;color:#fff;padding:10px 18px;"
        f"border-radius:6px;text-decoration:none'>{label}</a></p>"
        f"<p style='color:#666;font-size:12px'>{link}</p>"
    )


logger = logging.getLogger("hamdaz.quoting")

def _is_bid(request: QuoteRequest) -> bool:
    """Whether there is a bid pack worth attaching.

    The same question the screen asks before showing the bid sheets, and asked
    the same way: a quote raised because somebody rang up and asked for a price
    has none of this, and should not arrive with five sheets of blanks.
    """
    return bool(
        request.rfp_number
        or request.line_item_ref
        or request.incoterm_required
        or request.compliance
        or request.cost_lines
        or request.submission_fields
    )


class QuoteMailer(GraphMailer):
    """Every message a quote sends, each one from the person who caused it."""

    async def send_for_approval(
        self, request: QuoteRequest, recipients: list[str], *, link: str
    ) -> dict[str, Any]:
        """Tell the approvers a quote is waiting, with a way straight to it.

        The bid pack goes with it as a workbook. Approvers read mail on phones
        and between meetings, and a link that needs a sign-in is a decision
        deferred; the attachment is the whole bid — compliance, landed cost,
        margin ladder and the portal answers — readable without logging in
        anywhere. The link is still the thing to act on, and the mail says so.

        Only for a quote that has a bid behind it. Attaching five sheets of
        mostly-empty workbook to an ordinary estimate would be noise, so the
        workbook is built only when there is a bid pack to put in it.

        **Generating it never stops the mail.** If the workbook fails to build,
        the approvers are still told — being notified matters more than the copy
        they could have opened, and a quote silently waiting because a
        spreadsheet would not render is the worse failure by a distance.
        """
        attachments: list[Attachment] = []
        if _is_bid(request):
            try:
                attachments.append(
                    Attachment(
                        name=workbook.filename_for(request),
                        content=workbook.build(request),
                        content_type=XLSX_TYPE,
                    )
                )
            except Exception:  # noqa: BLE001 — see the docstring.
                logger.exception(
                    "bid workbook for quote %s could not be built; "
                    "sending the approval mail without it",
                    request.id,
                )

        return await self.send(
            sender=request.created_by.entra_object_id if request.created_by else "",
            recipients=recipients,
            subject=_subject(request),
            html=_body(request, link),
            attachments=attachments or None,
        )

    async def send_decision(
        self,
        request: QuoteRequest,
        recipients: list[str],
        *,
        link: str,
        review: QuoteReview,
    ) -> dict[str, Any]:
        """Tell the requester what was decided, and why if there is a why.

        Sent as the person who decided it. A rejection that arrives from a
        system address is an argument nobody can have; one that arrives from the
        approver is a conversation.
        """
        who = review.reviewer.display_name if review.reviewer else "An approver"
        what = _what(review)
        heading = "What they said" if review.action == "approve" else "Why"
        return await self.send(
            sender=review.reviewer.entra_object_id if review.reviewer else "",
            recipients=recipients,
            subject=(
                f"Quote {what} — {request.reference or request.title} — "
                f"{request.customer_name}"
            ),
            html=(
                f"<p>{who} {what} your quote for <b>{request.customer_name}</b> "
                f"({_money(request.total, request.currency)}).</p>"
                + _note_block(review.note, heading)
                + _link_block(link, "Open the quote")
            ),
        )

    async def send_comment(
        self,
        request: QuoteRequest,
        recipients: list[str],
        *,
        link: str,
        comment: QuoteComment,
    ) -> dict[str, Any]:
        """Pass a remark to the other side of the quote.

        Anchored comments are the useful ones, so the message says what it is
        attached to — a remark about a rate read out of context is just noise.
        """
        who = comment.author.display_name if comment.author else "Somebody"
        where = {
            "field": f"on {comment.target_ref}",
            "item": "on a line",
            "supplier_quote": "on a supplier quote",
        }.get(str(comment.target_type), "")
        return await self.send(
            sender=comment.author.entra_object_id if comment.author else "",
            recipients=recipients,
            subject=(
                f"Comment on quote {request.reference or request.title} — "
                f"{request.customer_name}"
            ),
            html=(
                f"<p>{who} commented {where} on the quote for "
                f"<b>{request.customer_name}</b>.</p>".replace("  ", " ")
                + _note_block(comment.body, "They said")
                + _link_block(link, "Open the quote")
            ),
        )
