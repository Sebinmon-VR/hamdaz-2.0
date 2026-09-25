"""Filing a quote's documents into the task's folder, and reading the other kinds.

The library is stubbed at the HTTP layer — an ``httpx.MockTransport`` that
answers as Graph does — so the folder matching, the path addressing and the
chunked upload are exercised as written, against nothing live. The one rule
that never bends here: nothing in this file, or in any session, writes to the
real library. See the never-write-to-SharePoint note.
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from decimal import Decimal

import httpx
import pytest

from app.core.config import get_settings
from app.models.quoting import CostStage, DocumentKind, QuoteDocument, QuoteRequest
from app.quoting import reading, storage
from app.quoting.service import QuoteError
from app.quoting.storage import DriveError, QuoteDrive

DRIVE = "b!GUSLy45PyUGtJDd3J89eeYaH7Z2XFJ9IpTydMWvJt-eWH0ljFA62TbLTydbo8eBd"
ROOT = "Proposal Team Channel"


class FakeGraph:
    """Just enough of Graph: a token, a folder tree, path-addressed uploads."""

    def __init__(self, folders: list[str]) -> None:
        self.folders = folders
        self.puts: list[tuple[str, int]] = []
        self.chunks: list[str] = []
        self.deleted: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/oauth2/v2.0/token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if "/createUploadSession" in url:
            return httpx.Response(200, json={"uploadUrl": "https://upload.test/session"})
        if url.startswith("https://upload.test/session"):
            self.chunks.append(request.headers["Content-Range"])
            end, total = request.headers["Content-Range"].split(" ")[1].split("/")
            if int(end.split("-")[1]) + 1 == int(total):
                return httpx.Response(201, json={"id": "big1", "webUrl": "https://sp/big"})
            return httpx.Response(202, json={})
        if request.method == "PUT" and url.endswith(":/content"):
            path = httpx.URL(url).path.split("/root:/")[1].removesuffix(":/content")
            self.puts.append((path, len(request.content)))
            return httpx.Response(201, json={"id": f"item-{len(self.puts)}", "webUrl": f"https://sp/{path}"})
        if request.method == "DELETE":
            self.deleted.append(url.rsplit("/", 1)[1])
            return httpx.Response(204)
        if "/search(q=" in url:
            q = url.split("search(q='")[1].split("')")[0]
            hits = [
                {
                    "id": f"f-{i}",
                    "name": name,
                    "folder": {},
                    "webUrl": f"https://sp/{name}",
                    "parentReference": {"path": f"/drives/{DRIVE}/root:/{ROOT}"},
                    "lastModifiedDateTime": f"2026-09-{10 + i:02d}",
                }
                for i, name in enumerate(self.folders)
                if q in name
            ]
            return httpx.Response(200, json={"value": hits})
        if request.method == "GET" and "/root:/" in url:
            path = httpx.URL(url).path.split("/root:/")[1].rstrip(":")
            name = path.split("/")[-1]
            if path.startswith(ROOT + "/") and name in self.folders:
                return httpx.Response(200, json={"id": "x", "name": name, "folder": {}, "webUrl": f"https://sp/{name}"})
            return httpx.Response(404, json={"error": {"code": "itemNotFound"}})
        return httpx.Response(500, json={"error": "unexpected"})


def drive_with(folders: list[str]) -> tuple[QuoteDrive, FakeGraph]:
    settings = get_settings().model_copy()
    settings.quote_drive_id = DRIVE
    settings.quote_drive_folder = ROOT
    graph = FakeGraph(folders)
    client = httpx.AsyncClient(transport=httpx.MockTransport(graph.handler))
    return QuoteDrive(settings, client), graph


# ── names ──────────────────────────────────────────────────────────────


def test_a_quote_folder_is_named_after_its_reference() -> None:
    assert storage.quote_folder_name("QT-001720", uuid.uuid4()) == "Quote request QT-001720"


def test_a_quote_with_no_reference_uses_a_short_id() -> None:
    request_id = uuid.UUID("3f2a9c1d-0000-0000-0000-000000000000")
    assert storage.quote_folder_name(None, request_id) == "Quote request 3f2a9c1d"


def test_file_names_lead_with_the_kind() -> None:
    assert storage.document_file_name("Supplier quote", "router-switch 4471.pdf") == (
        "Supplier quote — router-switch 4471.pdf"
    )


def test_illegal_characters_are_replaced_not_dropped() -> None:
    assert storage.safe_name('RFQ: "Toner" <urgent>/2026') == "RFQ- -Toner- -urgent-2026"


def test_the_event_number_at_the_front_of_a_title_is_found() -> None:
    title = "6000150626 AP Connect for ADNOC Central Laboratory"
    assert storage.leading_number(title) == "6000150626"
    assert storage.leading_number("RFQ 6000151176 supply of VISHAY") == "6000151176"
    assert storage.leading_number("Doc334974796 Digital Transformation") == "Doc334974796"
    assert storage.leading_number("RFQ for Toner") is None
    assert storage.leading_number("BBC MERSIN PROSPERITY - Battery telephone") is None


# ── finding the task's folder ──────────────────────────────────────────


async def test_a_folder_named_after_the_title_is_used_as_is() -> None:
    drive, _ = drive_with(["6000150626 AP Connect for ADNOC Central Laboratory"])
    match = await drive.find_task_folder("6000150626 AP Connect for ADNOC Central Laboratory")
    assert match.how == "exact"
    assert match.name == "6000150626 AP Connect for ADNOC Central Laboratory"


async def test_a_renamed_folder_is_found_by_its_event_number() -> None:
    """The team renames folders; the number at the front survives the rename."""
    drive, _ = drive_with(["6000151176 RFQ 6000151176 - VISHAY (revised)"])
    match = await drive.find_task_folder("RFQ 6000151176 supply of VISHAY ELECTRONIC GMBH")
    assert match.how == "number"
    assert match.name == "6000151176 RFQ 6000151176 - VISHAY (revised)"


async def test_the_newest_of_two_matching_folders_wins() -> None:
    drive, _ = drive_with(["6000151176 old", "6000151176 new"])
    match = await drive.find_task_folder("6000151176 whatever")
    assert match.name == "6000151176 new"


async def test_a_task_with_no_folder_gets_one_named_from_its_title() -> None:
    drive, _ = drive_with([])
    match = await drive.find_task_folder("RFQ for Toner")
    assert (match.name, match.how) == ("RFQ for Toner", "created")


async def test_a_number_that_matches_nothing_falls_back_to_creating() -> None:
    drive, _ = drive_with(["6000150626 AP Connect"])
    match = await drive.find_task_folder("6000159999 Something new")
    assert match.how == "created"


# ── putting a file there ───────────────────────────────────────────────


async def test_a_small_file_goes_in_one_request_under_the_quote_folder() -> None:
    drive, graph = drive_with([])
    filed = await drive.file_document(
        folder="RFQ for Toner/Quote request QT-001720",
        filename="Supplier quote — a.pdf",
        content=b"%PDF",
        content_type="application/pdf",
    )
    assert graph.puts == [
        ("Proposal Team Channel/RFQ for Toner/Quote request QT-001720/Supplier quote — a.pdf", 4)
    ]
    assert filed.item_id == "item-1"
    assert filed.path.startswith("Proposal Team Channel/")


async def test_a_large_file_goes_through_an_upload_session_in_chunks() -> None:
    drive, graph = drive_with([])
    content = b"x" * (storage.SIMPLE_UPLOAD_LIMIT + storage.UPLOAD_CHUNK + 10)
    filed = await drive.file_document(folder="T/Q", filename="scan.pdf", content=content)
    assert filed.item_id == "big1"
    assert graph.puts == []
    assert len(graph.chunks) == 2
    assert graph.chunks[0] == f"bytes 0-{storage.UPLOAD_CHUNK - 1}/{len(content)}"
    assert graph.chunks[-1].endswith(f"-{len(content) - 1}/{len(content)}")


async def test_a_file_over_the_ceiling_is_refused_before_anything_is_sent() -> None:
    drive, graph = drive_with([])
    with pytest.raises(DriveError, match="over the 60 MB limit"):
        await drive.file_document(
            folder="T/Q", filename="huge.zip", content=b"x" * (storage.MAX_UPLOAD_BYTES + 1)
        )
    assert graph.puts == [] and graph.chunks == []


async def test_nothing_is_filed_when_no_library_is_configured() -> None:
    settings = get_settings().model_copy()
    settings.quote_drive_id = ""
    transport = httpx.MockTransport(lambda r: httpx.Response(500))
    drive = QuoteDrive(settings, httpx.AsyncClient(transport=transport))
    assert drive.enabled is False
    with pytest.raises(DriveError):
        await drive.file_document(folder="T/Q", filename="a.pdf", content=b"x")
    assert await drive.try_file(folder="T/Q", filename="a.pdf", content=b"x") is None


# ── reading the other kinds ────────────────────────────────────────────


def request_with(**overrides) -> QuoteRequest:
    fields = {
        "title": "AP Connect", "customer_name": "ADNOC", "currency": "USD",
        "customs_duty_percent": Decimal(0), "financing_rate_percent": Decimal(0),
        "cash_exposure_days": 0, "discloses_principal_price": False,
        "multiple_supplier_quotes": False, "discount": Decimal(0),
        "shipping_charge": Decimal(0), "adjustment": Decimal(0), **overrides,
    }
    request = QuoteRequest(**fields)
    request.items, request.cost_lines, request.compliance = [], [], []
    request.submission_fields, request.documents = [], []
    return request


RFQ_CSV = b"""ADNOC Central Laboratory
RFQ No: 6000150626
Closing date: 12/10/2026
Delivery to: Ruwais - Abu Dhabi
Incoterm: DDP Ruwais
Bid validity: 90 days
Item,Description,Qty,Unit
1,AP Connect licence 1 year,4,each
2,AP Connect training,1,lot
"""


async def test_an_rfq_gives_its_reference_closing_date_and_place_but_not_its_items() -> None:
    request = request_with()
    document = QuoteDocument(kind=DocumentKind.CUSTOMER_RFQ, file_name="rfq.csv")
    await reading.read_into(document, request, "rfq.csv", RFQ_CSV, "text/csv")

    s = document.suggestions
    assert s["reference_number"]["value"] == "6000150626"
    assert s["cf_bcd"]["value"] == "2026-10-12"
    assert s["ship_to"]["value"] == "Ruwais - Abu Dhabi"
    assert s["place_of_supply"]["value"] == "Ruwais - Abu Dhabi"
    assert s["incoterm_required"]["value"] == "DDP"
    assert s["incoterm_place"]["value"] == "Ruwais"
    assert s["bid_validity_days"]["value"] == 90
    # The RFQ's item table is not offered: the lines come from the supplier's
    # quotation and nowhere else. What the customer asked for is not what we
    # are selling them until a supplier has priced it.
    assert "items" not in s
    assert all(not v["applied"] for v in s.values())


async def test_a_suggestion_that_matches_the_quote_already_reads_as_applied() -> None:
    request = request_with(reference_number="6000150626")
    document = QuoteDocument(kind=DocumentKind.CUSTOMER_RFQ, file_name="rfq.csv")
    await reading.read_into(document, request, "rfq.csv", RFQ_CSV, "text/csv")
    assert document.suggestions["reference_number"]["applied"] is True
    assert document.suggestions["cf_bcd"]["applied"] is False


async def test_applying_fills_blanks_and_leaves_the_lines_alone() -> None:
    request = request_with()
    document = QuoteDocument(kind=DocumentKind.CUSTOMER_RFQ, file_name="rfq.csv")
    await reading.read_into(document, request, "rfq.csv", RFQ_CSV, "text/csv")

    applied = reading.apply(request, document, ["reference_number", "cf_bcd", "bid_validity_days"])

    assert applied == ["reference_number", "cf_bcd", "bid_validity_days"]
    assert request.reference_number == "6000150626"
    assert request.cf_bcd == date(2026, 10, 12)
    assert request.bid_validity_days == 90
    assert request.items == []
    assert document.suggestions["cf_bcd"]["applied"] is True
    # And asking for the lines is refused, since none were offered.
    with pytest.raises(QuoteError, match="not something this document suggested"):
        reading.apply(request, document, ["items"])


async def test_a_typed_answer_is_not_written_over_unless_asked() -> None:
    request = request_with(reference_number="MR-TRJ-24-01-0790")
    document = QuoteDocument(kind=DocumentKind.CUSTOMER_RFQ, file_name="rfq.csv")
    await reading.read_into(document, request, "rfq.csv", RFQ_CSV, "text/csv")
    with pytest.raises(QuoteError, match="already filled in"):
        reading.apply(request, document, ["reference_number"])
    reading.apply(request, document, ["reference_number"], overwrite=True)
    assert request.reference_number == "6000150626"


async def test_a_courier_quote_becomes_a_firm_freight_row() -> None:
    request = request_with(currency="USD")
    document = QuoteDocument(kind=DocumentKind.FREIGHT_QUOTE, file_name="dhl.csv")
    await reading.read_into(
        document, request, "dhl.csv",
        b"DHL Express\nTransit time: 3-5 working days\nService,Amount\n"
        b"Express worldwide,USD 60.00\nTotal,USD 60.00\n",
        "text/csv",
    )
    assert document.suggestions["freight"]["value"] == "60.00"
    assert document.suggestions["freight"]["carrier"] == "DHL Express"

    reading.apply(request, document, ["freight"])

    row = request.cost_lines[0]
    assert row.label == "Freight – DHL Express"
    assert row.stage == CostStage.ORIGIN
    assert row.amount_base == Decimal("60.00") and row.is_firm is True
    assert "3-5 working days" in row.basis


async def test_a_po_gives_its_number_and_value() -> None:
    request = request_with()
    document = QuoteDocument(kind=DocumentKind.END_USER_PO, file_name="po.csv")
    await reading.read_into(
        document, request, "po.csv",
        b"ADNOC Onshore\nPurchase Order No: 4500123456\nPO Date: 20 Sep 2026\n"
        b"Line,Amount\n1,3988.94\nTotal,USD 3988.94\n",
        "text/csv",
    )
    s = document.suggestions
    assert s["reference_number"]["value"] == "4500123456"
    assert s["po_date"]["value"] == "2026-09-20"
    assert s["po_value"]["value"] == "3988.94"


async def test_a_datasheet_is_filed_and_read_for_nothing() -> None:
    request = request_with()
    document = QuoteDocument(kind=DocumentKind.TECHNICAL_SPEC, file_name="spec.csv")
    await reading.read_into(document, request, "spec.csv", b"Spec,Value\nWeight,2kg\n", "text/csv")
    assert document.extracted["read"] is True
    assert document.suggestions is None


async def test_a_scan_is_filed_with_a_note_that_it_was_not_read() -> None:
    request = request_with()
    document = QuoteDocument(kind=DocumentKind.CUSTOMER_RFQ, file_name="scan.png")
    await reading.read_into(
        document, request, "scan.png", b"\x89PNG\r\n\x1a\n" + b"x" * 100, "image/png"
    )
    assert document.extracted["read"] is False
    assert "scan" in document.extracted["why"]


def test_dates_are_read_day_first_as_the_gulf_writes_them() -> None:
    assert reading.parse_date("04/10/2026") == date(2026, 10, 4)
    assert reading.parse_date("4th October 2026") == date(2026, 10, 4)
    assert reading.parse_date("Oct 4, 2026") == date(2026, 10, 4)
    assert reading.parse_date("2026-10-04") == date(2026, 10, 4)
    assert reading.parse_date("next week") is None


def test_the_document_summary_is_json_shaped(monkeypatch) -> None:
    from app.quoting import filing

    document = QuoteDocument(kind=DocumentKind.OTHER, file_name="a.pdf", size=3)
    document.id = uuid.uuid4()
    document.created_at = None
    row = filing.summary(document)
    json.dumps({k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in row.items()})
    assert row["kind_label"] == "Document"
