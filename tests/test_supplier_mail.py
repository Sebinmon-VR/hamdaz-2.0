"""The supplier's email: picked from your own mailbox, filed, sent to the approvers.

The mailbox and the library are stubs. Nothing here reads a real mailbox or
writes to SharePoint, and the approval mail goes to a recording mailer.
"""

from __future__ import annotations

import io

from app.quoting import supplier_mail
from tests.test_quoting_documents_routes import RecordingDrive
from tests.test_quoting_routes import (  # noqa: F401 - the fixtures come along
    API,
    FORM,
    _as,
    _priced,
    quoting,
    requester,
    team,
)

EML = (
    b"From: Ahmed Khan <ahmed@supplier.example>\r\n"
    b"To: Engineer <engineer@hamdaz.com>\r\n"
    b"Subject: RE: Offer for HPE drives\r\n"
    b"Date: Tue, 29 Sep 2026 10:15:00 +0400\r\n"
    b"MIME-Version: 1.0\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"Dear Engineer,\r\n\r\nPrice USD 637 each. Customs duty extra.\r\n<script>x</script>\r\n"
)

MESSAGE = {
    "id": "AAMk-immutable-1",
    "subject": "RE: Offer for HPE drives",
    "from": {"emailAddress": {"name": "Ahmed Khan", "address": "ahmed@supplier.example"}},
    "toRecipients": [{"emailAddress": {"name": "Engineer", "address": "engineer@hamdaz.com"}}],
    "ccRecipients": [],
    "sentDateTime": "2026-09-29T06:15:00Z",
    "receivedDateTime": "2026-09-29T06:15:02Z",
    "bodyPreview": "Dear Engineer, Price USD 637 each.",
    "body": {"contentType": "text", "content": "Dear Engineer,\r\n\r\nPrice USD 637 each."},
    "hasAttachments": True,
}


class StubMailbox:
    """A mailbox, as far as the routes can tell. Records whose it was asked for."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def search(self, mailbox, query, *, fields, top=25):
        self.asked.append(mailbox)
        return [MESSAGE]

    async def full_message(self, mailbox, message_id):
        self.asked.append(mailbox)
        return MESSAGE

    async def attachment_names(self, mailbox, message_id):
        return ["Quotation 1182.pdf"]

    async def mime(self, mailbox, message_id):
        return EML


class DownloadingDrive(RecordingDrive):
    def __init__(self, folders):
        super().__init__(folders)
        self.store: dict[str, bytes] = {}

    async def file_document(self, *, folder, filename, content, content_type=None):
        filed = await super().file_document(
            folder=folder, filename=filename, content=content, content_type=content_type
        )
        self.store[filed.item_id] = content
        return filed

    async def download(self, item_id: str) -> bytes:
        return self.store[item_id]


# ── reading and formatting ─────────────────────────────────────────────


def test_a_dropped_eml_is_read_for_who_when_and_what() -> None:
    email = supplier_mail.from_eml(EML)
    assert email["from"] == {"name": "Ahmed Khan", "address": "ahmed@supplier.example"}
    assert email["subject"] == "RE: Offer for HPE drives"
    assert email["sent"].startswith("2026-09-29T10:15")
    assert "Customs duty extra." in email["body"]


def test_something_that_is_not_an_email_is_not_read_as_one() -> None:
    assert supplier_mail.from_eml(b"%PDF-1.4 not a message") is None


def test_the_card_escapes_the_suppliers_text() -> None:
    card = supplier_mail.card(supplier_mail.from_eml(EML), link="https://sp/x.eml")
    assert "&lt;script&gt;" in card and "<script>" not in card
    assert "Ahmed Khan &lt;ahmed@supplier.example&gt;" in card
    assert "Tue 29 Sep 2026, 10:15" in card
    assert "Open the original in SharePoint" in card


# ── over HTTP ──────────────────────────────────────────────────────────


async def test_the_picker_reads_only_the_callers_own_mailbox(quoting, requester, team) -> None:
    mailbox = StubMailbox()
    quoting._transport.app.state.mail_reader = mailbox
    created = (await _as(quoting, requester).post(f"{API}?team={team.id}", json=FORM)).json()

    listed = await quoting.get(f"{API}/{created['id']}/mailbox", params={"q": "HPE"})

    assert listed.status_code == 200, listed.text
    assert listed.json()[0]["from_address"] == "ahmed@supplier.example"
    assert mailbox.asked == [requester.entra_object_id or requester.email]


async def test_a_picked_email_goes_to_the_approvers_formatted_and_attached(
    quoting, requester, team
) -> None:
    drive = DownloadingDrive([])
    quoting._transport.app.state.quote_drive = drive
    quoting._transport.app.state.mail_reader = StubMailbox()
    quote_id = await _priced(quoting, requester, team)

    attached = await quoting.post(
        f"{API}/{quote_id}/supplier-emails", json={"message_id": "AAMk-immutable-1"}
    )
    assert attached.status_code == 200, attached.text
    doc = next(d for d in attached.json()["documents"] if d["kind"] == "supplier_email")
    assert doc["kind_label"] == "Supplier email"
    assert doc["file_name"] == "RE Offer for HPE drives.eml"
    assert doc["email"]["from"]["address"] == "ahmed@supplier.example"
    assert doc["email"]["attachments"] == ["Quotation 1182.pdf"]

    mailer = quoting._transport.app.state.quote_mailer
    mailer.sent.clear()
    sent = await quoting.post(f"{API}/{quote_id}/submit")
    assert sent.status_code == 200, sent.text

    mail = mailer.sent[-1]
    assert "The supplier's email" in mail["html"]
    assert "Price USD 637 each." in mail["html"]
    assert "RE Offer for HPE drives.eml" in mail["attachments"]


async def test_a_dropped_eml_is_filed_as_a_supplier_email(quoting, requester, team) -> None:
    quoting._transport.app.state.quote_drive = RecordingDrive([])
    created = (await _as(quoting, requester).post(f"{API}?team={team.id}", json=FORM)).json()

    response = await quoting.post(
        f"{API}/{created['id']}/documents",
        data={"kind": "supplier_email"},
        files=[("files", ("offer.eml", io.BytesIO(EML), "message/rfc822"))],
    )

    assert response.status_code == 200, response.text
    doc = response.json()["documents"][0]
    assert doc["kind"] == "supplier_email"
    assert doc["email"]["subject"] == "RE: Offer for HPE drives"
