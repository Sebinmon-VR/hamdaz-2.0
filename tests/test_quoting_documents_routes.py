"""Documents on a quote over HTTP: uploaded, filed, read, applied, removed.

The drive is a stub that records what it was asked to file and answers as
the library would. Nothing here reaches SharePoint — the never-write rule —
and the stub is switched on so the filing path runs as written: the task
folder is found, the quote folder is named, the file lands under both.
"""

from __future__ import annotations

import io

from app.models.proposal_index import ProposalIndexItem
from app.quoting.storage import FiledDocument, FolderMatch
from tests.test_quoting_routes import (  # noqa: F401 - the fixtures come along
    API,
    FORM,
    _as,
    _extracted,
    quoting,
    requester,
    team,
)

RFQ_CSV = b"""ADNOC Central Laboratory
RFQ No: 6000150626
Closing date: 12/10/2026
Delivery to: Ruwais - Abu Dhabi
Bid validity: 90 days
Item,Description,Qty,Unit
1,AP Connect licence 1 year,4,each
2,AP Connect training,1,lot
"""


class RecordingDrive:
    """The library, as far as the routes can tell: a folder per task, and a
    record of every file put in one."""

    enabled = True
    root = "Proposal Team Channel"
    unlinked_root = "_Quotes without a task"

    def __init__(self, folders: list[str]) -> None:
        self.folders = folders
        self.filed: list[tuple[str, str, int]] = []
        self.deleted: list[str] = []

    async def find_task_folder(self, title: str) -> FolderMatch:
        if title in self.folders:
            return FolderMatch(title, "exact", f"https://sp/{title}")
        return FolderMatch(title, "created", None)

    async def file_document(
        self, *, folder, filename, content, content_type=None
    ) -> FiledDocument:
        self.filed.append((folder, filename, len(content)))
        path = f"{self.root}/{folder}/{filename}"
        return FiledDocument(
            item_id=f"item-{len(self.filed)}", web_url=f"https://sp/{path}", path=path
        )

    async def try_file(self, **kw) -> FiledDocument | None:
        return await self.file_document(**kw)

    async def folder_url(self, path: str) -> str:
        return f"https://sp/{path}"

    async def delete_item(self, item_id: str) -> None:
        self.deleted.append(item_id)


async def _with_task(db, client, user, the_team, title: str):
    """A quote raised against a mirrored task, so filing has a folder to find."""
    db.add(
        ProposalIndexItem(
            item_id="4242", title=title, is_open=True, is_active=True,
            has_attachments=False, search_text="",
        )
    )
    await db.commit()
    created = (await _as(client, user).post(f"{API}?team={the_team.id}", json=FORM)).json()
    # The route that raises from a task is exercised elsewhere; here the link
    # is set the way that route sets it.
    from app.models.quoting import QuoteRequest

    row = await db.get(QuoteRequest, __import__("uuid").UUID(created["id"]))
    row.source_task_id = "4242"
    await db.commit()
    return created


async def test_an_rfq_is_filed_in_the_tasks_folder_and_read_into_suggestions(
    quoting, db, requester, team
) -> None:
    drive = RecordingDrive(["6000150626 AP Connect for ADNOC Central Laboratory"])
    quoting._transport.app.state.quote_drive = drive
    created = await _with_task(
        db, quoting, requester, team, "6000150626 AP Connect for ADNOC Central Laboratory"
    )

    response = await quoting.post(
        f"{API}/{created['id']}/documents",
        data={"kind": "customer_rfq", "notes": "Received by email"},
        files=[("files", ("rfq.csv", io.BytesIO(RFQ_CSV), "text/csv"))],
    )

    assert response.status_code == 200, response.text
    body = response.json()
    # Filed under the task's folder, in the quote's own subfolder, named by kind.
    folder, name, size = drive.filed[0]
    assert folder.startswith("6000150626 AP Connect for ADNOC Central Laboratory/Quote request ")
    assert (name, size) == ("Customer RFQ — rfq.csv", len(RFQ_CSV))
    assert body["drive_folder"].startswith("6000150626 AP Connect")
    assert body["drive_folder_url"]
    docs = body["documents"]
    assert len(docs) == 1
    doc = docs[0]
    assert doc["kind"] == "customer_rfq" and doc["kind_label"] == "Customer RFQ"
    assert doc["drive_url"].startswith("https://sp/")
    assert doc["uploaded_by_name"] == "Engineer"
    assert doc["notes"] == "Received by email"
    # And what it says, offered rather than applied.
    s = doc["suggestions"]
    assert s["cf_bcd"]["value"] == "2026-10-12"
    assert s["bid_validity_days"]["value"] == 90
    # The lines come from the supplier's quotation only; an RFQ's items are
    # not offered, whatever its table says.
    assert "items" not in s
    assert s["cf_bcd"]["applied"] is False


async def test_applying_suggestions_writes_them_onto_the_quote(
    quoting, db, requester, team
) -> None:
    quoting._transport.app.state.quote_drive = RecordingDrive([])
    created = (await _as(quoting, requester).post(f"{API}?team={team.id}", json=FORM)).json()
    uploaded = (
        await quoting.post(
            f"{API}/{created['id']}/documents",
            data={"kind": "customer_rfq"},
            files=[("files", ("rfq.csv", io.BytesIO(RFQ_CSV), "text/csv"))],
        )
    ).json()
    doc = uploaded["documents"][0]

    # The reference is already typed on this quote, so it is refused without
    # the switch; the closing date is blank and goes straight on.
    refused = await quoting.post(
        f"{API}/{created['id']}/documents/{doc['id']}/apply",
        json={"fields": ["reference_number"], "overwrite": False},
    )
    assert refused.status_code == 400
    assert "already filled in" in refused.json()["detail"]

    applied = await quoting.post(
        f"{API}/{created['id']}/documents/{doc['id']}/apply",
        json={"fields": ["cf_bcd", "bid_validity_days"], "overwrite": False},
    )
    assert applied.status_code == 200, applied.text
    body = applied.json()
    assert body["cf_bcd"].startswith("2026-10-12")
    assert body["bid_validity_days"] == 90
    assert body["documents"][0]["suggestions"]["cf_bcd"]["applied"] is True


async def test_a_quote_with_no_task_files_under_the_unlinked_folder(
    quoting, db, requester, team
) -> None:
    drive = RecordingDrive([])
    quoting._transport.app.state.quote_drive = drive
    created = (await _as(quoting, requester).post(f"{API}?team={team.id}", json=FORM)).json()

    response = await quoting.post(
        f"{API}/{created['id']}/documents",
        data={"kind": "technical_spec"},
        files=[("files", ("spec.pdf", io.BytesIO(b"%PDF-1.4 not really"), "application/pdf"))],
    )

    assert response.status_code == 200, response.text
    assert drive.filed[0][0].startswith("_Quotes without a task/Quote request ")
    assert drive.filed[0][1] == "Technical spec — spec.pdf"


async def test_a_document_that_cannot_be_filed_is_not_attached(
    quoting, db, requester, team
) -> None:
    from app.quoting.storage import DriveError

    class RefusingDrive(RecordingDrive):
        async def file_document(self, **kw):
            raise DriveError("locked")

    quoting._transport.app.state.quote_drive = RefusingDrive([])
    created = (await _as(quoting, requester).post(f"{API}?team={team.id}", json=FORM)).json()

    response = await quoting.post(
        f"{API}/{created['id']}/documents",
        data={"kind": "other"},
        files=[("files", ("x.pdf", io.BytesIO(b"%PDF"), "application/pdf"))],
    )

    assert response.status_code == 502
    assert "could not be filed" in response.json()["detail"]
    after = (await quoting.get(f"{API}/{created['id']}")).json()
    assert after["documents"] == []


async def test_a_supplier_quote_upload_is_filed_and_listed_as_one(
    quoting, db, requester, team
) -> None:
    drive = RecordingDrive([])
    quoting._transport.app.state.quote_drive = drive
    quoting._transport.app.state.quote_extractor.results = [_extracted("Alpha Trading", 12000)]
    created = (await _as(quoting, requester).post(f"{API}?team={team.id}", json=FORM)).json()

    response = await quoting.post(
        f"{API}/{created['id']}/supplier-quotes",
        files=[
            (
                "files",
                ("alpha.csv", io.BytesIO(b"Item,Qty,Price\nFG-201G,2,12000\n"), "text/csv"),
            ),
        ],
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert drive.filed[0][1] == "Supplier quote — alpha.csv"
    docs = body["documents"]
    assert len(docs) == 1
    assert docs[0]["kind"] == "supplier_quote"
    assert docs[0]["supplier_name"] == "Alpha Trading"
    assert docs[0]["supplier_quote_id"] == body["comparison"]["suppliers"][0]["quote_id"]


async def test_removing_a_supplier_quote_takes_its_prices_out_of_the_comparison(
    quoting, db, requester, team
) -> None:
    """Two suppliers compared; one removed. The comparison is worked out again
    over the one left, and the file goes from the folder."""
    drive = RecordingDrive([])
    quoting._transport.app.state.quote_drive = drive
    quoting._transport.app.state.quote_extractor.results = [
        _extracted("Alpha Trading", 12000),
        _extracted("Beta Supplies", 11000),
    ]
    created = (await _as(quoting, requester).post(f"{API}?team={team.id}", json=FORM)).json()
    attached = (
        await quoting.post(
            f"{API}/{created['id']}/supplier-quotes",
            files=[
                (
                    "files",
                    ("alpha.csv", io.BytesIO(b"Item,Qty,Price\nFG-201G,2,12000\n"), "text/csv"),
                ),
                (
                    "files",
                    ("beta.csv", io.BytesIO(b"Item,Qty,Price\nFG-201G,2,11000\n"), "text/csv"),
                ),
            ],
        )
    ).json()
    assert attached["comparison"]["supplier_count"] == 2
    alpha = next(d for d in attached["documents"] if d["supplier_name"] == "Alpha Trading")

    response = await quoting.delete(f"{API}/{created['id']}/documents/{alpha['id']}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert [d["supplier_name"] for d in body["documents"]] == ["Beta Supplies"]
    assert body["comparison"]["supplier_count"] == 1
    assert body["comparison"]["suppliers"][0]["supplier_name"] == "Beta Supplies"
    assert drive.deleted == ["item-1"]

    # And the last one: the comparison goes with it.
    beta = body["documents"][0]
    last = (await quoting.delete(f"{API}/{created['id']}/documents/{beta['id']}")).json()
    assert last["documents"] == []
    assert last["comparison"] is None and last["comparison_id"] is None
    assert last["multiple_supplier_quotes"] is False


async def test_removing_the_chosen_supplier_clears_the_choice_but_keeps_the_lines(
    quoting, db, requester, team
) -> None:
    from tests.test_quoting_routes import _priced

    drive = RecordingDrive([])
    quoting._transport.app.state.quote_drive = drive
    quote_id = await _priced(quoting, requester, team)
    before = (await quoting.get(f"{API}/{quote_id}")).json()
    chosen = next(d for d in before["documents"] if d["is_selected"])
    assert before["may_submit"] is True

    after = (await quoting.delete(f"{API}/{quote_id}/documents/{chosen['id']}")).json()

    assert after["selected_supplier_quote_id"] is None
    assert [i["name"] for i in after["items"]] == [i["name"] for i in before["items"]]
    assert after["may_submit"] is False
    assert "none has been chosen" in after["submit_reason"]


async def test_removing_a_document_takes_it_out_of_the_folder_too(
    quoting, db, requester, team
) -> None:
    drive = RecordingDrive([])
    quoting._transport.app.state.quote_drive = drive
    created = (await _as(quoting, requester).post(f"{API}?team={team.id}", json=FORM)).json()
    uploaded = (
        await quoting.post(
            f"{API}/{created['id']}/documents",
            data={"kind": "other"},
            files=[("files", ("note.pdf", io.BytesIO(b"%PDF"), "application/pdf"))],
        )
    ).json()
    doc_id = uploaded["documents"][0]["id"]

    response = await quoting.delete(f"{API}/{created['id']}/documents/{doc_id}")

    assert response.status_code == 200, response.text
    assert response.json()["documents"] == []
    assert drive.deleted == ["item-1"]


async def test_a_wrong_kind_is_refused_with_the_list(quoting, db, requester, team) -> None:
    quoting._transport.app.state.quote_drive = RecordingDrive([])
    created = (await _as(quoting, requester).post(f"{API}?team={team.id}", json=FORM)).json()
    response = await quoting.post(
        f"{API}/{created['id']}/documents",
        data={"kind": "receipt"},
        files=[("files", ("x.pdf", io.BytesIO(b"%PDF"), "application/pdf"))],
    )
    assert response.status_code == 400
    assert "customer_rfq" in response.json()["detail"]


async def test_submitting_files_the_report_for_the_pass(quoting, db, requester, team) -> None:
    from tests.test_quoting_routes import _priced

    drive = RecordingDrive([])
    quoting._transport.app.state.quote_drive = drive
    quote_id = await _priced(quoting, requester, team)

    response = await quoting.post(f"{API}/{quote_id}/submit")

    assert response.status_code == 200, response.text
    body = response.json()
    reports = [d for d in body["documents"] if d["kind"] == "costing_report"]
    assert len(reports) == 1
    assert reports[0]["revision"] == 1
    assert reports[0]["file_name"].endswith("Selling & Costing Report pass 1.pdf")
    assert any(name.endswith("pass 1.pdf") for _, name, _ in drive.filed)
    assert body["filing_error"] is None

async def test_a_typed_offer_comes_off_the_comparison_by_its_own_id(
    quoting, db, requester, team
) -> None:
    """A typed-in offer has no document behind it, so the card's Remove uses
    the supplier quote's own id."""
    quoting._transport.app.state.quote_drive = RecordingDrive([])
    created = (await _as(quoting, requester).post(f"{API}?team={team.id}", json=FORM)).json()
    typed = (
        await quoting.post(
            f"{API}/{created['id']}/supplier-quotes/typed",
            json={"quotes": [
                {"supplier_name": "Yalla LLC", "currency": "AED",
                 "items": [{"description": "Toner", "quantity": "1", "unit_price": "300"}]},
            ]},
        )
    ).json()
    offer = typed["comparison"]["suppliers"][0]["quote_id"]

    response = await quoting.delete(f"{API}/{created['id']}/supplier-quotes/{offer}")

    assert response.status_code == 200, response.text
    assert response.json()["comparison"] is None
    gone = await quoting.delete(f"{API}/{created['id']}/supplier-quotes/{offer}")
    assert gone.status_code == 404

async def test_the_quote_takes_the_suppliers_currency(quoting, db, requester, team) -> None:
    """A dollar offer makes a dollar quote. Nothing is converted on the
    comparison; the quote itself switches, since nothing was priced on it."""
    quoting._transport.app.state.quote_drive = RecordingDrive([])
    offer = _extracted("Router-Switch", 637)
    offer.currency = "USD"
    quoting._transport.app.state.quote_extractor.results = [offer]
    form = {**FORM, "currency": "AED", "items": []}
    created = (await _as(quoting, requester).post(f"{API}?team={team.id}", json=form)).json()
    assert created["currency"] == "AED"

    response = await quoting.post(
        f"{API}/{created['id']}/supplier-quotes",
        files=[("files", ("rs.csv", io.BytesIO(b"Item,Qty,Price\nHDD,2,637\n"), "text/csv"))],
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["currency"] == "USD"
    assert body["comparison"]["currency"] == "USD"
    supplier = body["comparison"]["suppliers"][0]
    assert supplier["converted"] is False
    assert supplier["total"] == 637 * 2

async def test_an_offer_can_be_told_its_currency_when_the_document_does_not_say(
    quoting, db, requester, team
) -> None:
    """A screenshot with no currency on it used to be taken in the quote's own
    currency. Told it is in dollars, it is dollars, and the quote follows."""
    quoting._transport.app.state.quote_drive = RecordingDrive([])
    offer = _extracted("Router-Switch", 637)
    offer.currency = None
    quoting._transport.app.state.quote_extractor.results = [offer]
    form = {**FORM, "currency": "AED", "items": []}
    created = (await _as(quoting, requester).post(f"{API}?team={team.id}", json=form)).json()

    response = await quoting.post(
        f"{API}/{created['id']}/supplier-quotes",
        data={"currency": "usd"},
        files=[("files", ("rs.csv", io.BytesIO(b"Item,Qty,Price\nHDD,2,637\n"), "text/csv"))],
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["currency"] == "USD"
    assert body["comparison"]["suppliers"][0]["currency"] == "USD"
