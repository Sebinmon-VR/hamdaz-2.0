"""The supplier's own email, kept with the quote and sent on to the approvers.

A supplier's offer arrives as an email as often as a PDF: the price in the
body, the conditions in the thread, the quotation attached. The approvers
should read what the supplier actually said, not the requester's summary of it.

So the requester picks the message from **their own mailbox**. The picker
never reads anybody else's, even though the application's Graph permission
could. It is filed with the quote like any other document, as the original
``.eml``, with its sender, date, subject and text kept on the document row.
When the quote goes for approval, each of these emails is in the mail as a
formatted card, and the original goes as an attachment the approver can open
in Outlook, attachments and all.

A ``.eml`` dropped in by hand ("Save as" from Outlook) is read the same way.
A ``.msg`` is filed and attached, but its text is not read.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import datetime
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any, Final

#: What is kept of the body. Enough for any real offer; a forty-reply thread
#: below it is the thread, and the original is attached for that.
BODY_LIMIT: Final = 20_000

#: What the picker lists per message.
LIST_FIELDS: Final = (
    "id,subject,from,toRecipients,receivedDateTime,bodyPreview,hasAttachments"
)

EML_TYPE: Final = "message/rfc822"


@dataclass(frozen=True)
class SupplierEmail:
    """One supplier email as the approval mail needs it."""

    #: What was kept of it: see ``from_graph`` and ``from_eml``.
    email: dict[str, Any]
    #: Where it is filed in the shared library, when it is.
    link: str | None
    #: The original, when it could be read back; None sends the card alone.
    content: bytes | None
    file_name: str
    content_type: str = EML_TYPE


def _person(entry: dict[str, Any] | None) -> dict[str, str]:
    holder = (entry or {}).get("emailAddress") or {}
    return {
        "name": (holder.get("name") or "").strip(),
        "address": (holder.get("address") or "").strip(),
    }


def listing(raw: dict[str, Any]) -> dict[str, Any]:
    """One message as the picker shows it."""
    sender = _person(raw.get("from"))
    return {
        "id": raw.get("id"),
        "subject": raw.get("subject") or "(no subject)",
        "from_name": sender["name"] or None,
        "from_address": sender["address"] or None,
        "received": raw.get("receivedDateTime"),
        "preview": (raw.get("bodyPreview") or "")[:240],
        "has_attachments": bool(raw.get("hasAttachments")),
    }


def from_graph(raw: dict[str, Any], *, mailbox: str, attachment_names: list[str]) -> dict[str, Any]:
    """What is kept of a message picked from the mailbox."""
    body = ((raw.get("body") or {}).get("content") or "").strip()
    return {
        "source": "mailbox",
        "mailbox": mailbox,
        "message_id": raw.get("id"),
        "subject": raw.get("subject") or "",
        "from": _person(raw.get("from")),
        "to": [_person(p) for p in raw.get("toRecipients") or []],
        "cc": [_person(p) for p in raw.get("ccRecipients") or []],
        "sent": raw.get("sentDateTime") or raw.get("receivedDateTime"),
        "body": _tidy(body)[:BODY_LIMIT],
        "attachments": attachment_names,
    }


def from_eml(content: bytes) -> dict[str, Any] | None:
    """What is kept of a ``.eml`` dropped in by hand. None when it is not one."""
    try:
        message = BytesParser(policy=policy.default).parsebytes(content)
    except Exception:  # noqa: BLE001 - not an email; filed, not read
        return None
    if not (message.get("From") or message.get("Subject")):
        return None

    def people(header: str) -> list[dict[str, str]]:
        return [
            {"name": name, "address": address}
            for name, address in getaddresses(message.get_all(header, []))
            if address or name
        ]

    sent = None
    if message.get("Date"):
        try:
            sent = parsedate_to_datetime(str(message["Date"])).isoformat()
        except (TypeError, ValueError):
            sent = str(message["Date"])

    body = ""
    part = message.get_body(preferencelist=("plain", "html"))
    if part is not None:
        try:
            text = part.get_content()
        except (LookupError, UnicodeDecodeError):
            text = ""
        body = _strip_html(text) if part.get_content_subtype() == "html" else text
    attachments = [
        a.get_filename() or "attachment" for a in message.iter_attachments()
    ]
    senders = people("From")
    return {
        "source": "upload",
        "subject": str(message.get("Subject") or ""),
        "from": senders[0] if senders else {"name": "", "address": ""},
        "to": people("To"),
        "cc": people("Cc"),
        "sent": sent,
        "body": _tidy(body)[:BODY_LIMIT],
        "attachments": attachments,
    }


def _strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style|head).*?</\1>", "", text)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text)


def _tidy(text: str) -> str:
    """Line endings settled and runs of blank lines collapsed."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def file_name_for(email: dict[str, Any]) -> str:
    subject = re.sub(r"[\\/:*?\"<>|#%]+", " ", email.get("subject") or "Supplier email")
    subject = re.sub(r"\s+", " ", subject).strip()[:90] or "Supplier email"
    return f"{subject}.eml"


def _who(person: dict[str, str] | None) -> str:
    person = person or {}
    name, address = person.get("name") or "", person.get("address") or ""
    if name and address and name.lower() != address.lower():
        return f"{html.escape(name)} &lt;{html.escape(address)}&gt;"
    return html.escape(name or address or "—")


def _when(value: str | None) -> str:
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%a %d %b %Y, %H:%M")
    except ValueError:
        return html.escape(value)


def card(email: dict[str, Any], *, link: str | None = None) -> str:
    """The email as a card in the approval mail: the header, then what it says.

    Built for Outlook's renderer — tables and inline styles, nothing else —
    and escaped throughout, because the text is the supplier's, not ours.
    """
    rows = [
        ("From", _who(email.get("from"))),
        ("To", ", ".join(_who(p) for p in email.get("to") or []) or "—"),
    ]
    if email.get("cc"):
        rows.append(("Cc", ", ".join(_who(p) for p in email["cc"])))
    rows += [
        ("Date", _when(email.get("sent"))),
        ("Subject", html.escape(email.get("subject") or "(no subject)")),
    ]
    if email.get("attachments"):
        rows.append(("Attachments", html.escape(", ".join(email["attachments"]))))
    header = "".join(
        f"<tr><td style='padding:2px 12px 2px 0;color:#666;vertical-align:top;"
        f"white-space:nowrap'>{label}</td><td style='padding:2px 0'>{value}</td></tr>"
        for label, value in rows
    )
    body = html.escape(email.get("body") or "").replace("\n", "<br>")
    more = (
        f"<p style='margin:10px 0 0;font-size:12px'><a href='{html.escape(link)}'>"
        f"Open the original in SharePoint</a></p>"
        if link
        else ""
    )
    return (
        "<table cellpadding='0' cellspacing='0' width='100%' style='margin:14px 0;"
        "border:1px solid #e3e3e3;border-radius:8px;border-collapse:separate'>"
        "<tr><td style='padding:12px 16px;background:#f7f7f5;border-bottom:1px solid #e3e3e3;"
        "border-radius:8px 8px 0 0'>"
        f"<table cellpadding='0' cellspacing='0' style='font-size:13px'>{header}</table></td></tr>"
        "<tr><td style='padding:14px 16px;font-size:13px;line-height:1.5;color:#222'>"
        f"{body or '<i>No text in the message body.</i>'}{more}</td></tr></table>"
    )
