"""The comparison maths, and reading the files it starts from.

Nothing here touches a model — there is none. Matching items is done by part
numbers and words, and every number a buyer acts on is computed in Python, so
all of it can be tested without a network call or a bill.

The tests that matter most are the incomparability ones. A supplier who quoted
eight of ten lines has a smaller total than one who quoted all ten, and treating
that as "cheaper" is the expensive mistake this module exists to prevent.
"""

from __future__ import annotations

import io
from decimal import Decimal

import pytest

from app.comparison.analysis import _fallback_groups, analyse
from app.comparison.documents import DocumentError, prepare, resolve_type
from app.comparison.parsing import parse
from app.comparison.schemas import ItemIn, QuoteIn
from app.comparison.service import line_total, to_domain


def item(description: str, qty, price, *, part=None, total=None) -> ItemIn:
    return ItemIn(
        description=description,
        part_number=part,
        quantity=Decimal(str(qty)),
        unit_price=Decimal(str(price)),
        line_total=None if total is None else Decimal(str(total)),
    )


def quote(name: str, items: list[ItemIn], **kw) -> QuoteIn:
    return QuoteIn(supplier_name=name, items=items, **kw)


def run(quotes: list[QuoteIn], currency: str = "AED") -> dict:
    """Analyse with part-number matching, so no model is involved."""
    domain = to_domain(quotes)
    return analyse(domain, _fallback_groups(domain), currency=currency)


# ── the totals ─────────────────────────────────────────────────────────


def test_a_supplier_total_is_the_sum_of_their_lines() -> None:
    result = run([quote("Alpha", [item("Filter", 10, 120), item("Seal", 4, 85)])])
    assert result["suppliers"][0]["total"] == 10 * 120 + 4 * 85


def test_freight_and_tax_are_added_and_discount_taken_off() -> None:
    result = run(
        [
            quote(
                "Alpha",
                [item("Filter", 10, 100)],
                discount=Decimal("50"),
                freight=Decimal("200"),
                tax=Decimal("30"),
            )
        ]
    )
    assert result["suppliers"][0]["total"] == 1000 - 50 + 200 + 30


def test_money_does_not_drift(strict: None = None) -> None:
    """0.1 + 0.2 must be 0.3 here. These figures go back to a supplier."""
    result = run([quote("Alpha", [item("A", 1, "0.1"), item("B", 1, "0.2")])])
    assert result["suppliers"][0]["total"] == 0.3


def test_a_printed_line_total_beats_the_multiplication() -> None:
    """The supplier's own figure stands; the disagreement is reported, not fixed."""
    assert line_total(item("Filter", 10, 100, total=950)) == Decimal("950")
    assert line_total(item("Filter", 10, 100)) == Decimal("1000")


def test_a_printed_grand_total_that_disagrees_is_flagged() -> None:
    result = run([quote("Alpha", [item("Filter", 10, 100)], quoted_total=Decimal("900"))])

    supplier = result["suppliers"][0]
    assert supplier["total_mismatch"] == -100
    assert any(i["kind"] == "total_mismatch" for i in result["insights"])


# ── incomparability, which comes before price ──────────────────────────


def test_a_supplier_missing_a_line_is_flagged() -> None:
    result = run(
        [
            quote("Alpha", [item("Filter", 10, 120, part="FX"), item("Seal", 4, 85, part="SK")]),
            quote("Beta", [item("Filter", 10, 105, part="FX")]),
        ]
    )

    beta = next(s for s in result["suppliers"] if s["supplier_name"] == "Beta")
    assert beta["missing_items"] == ["Seal"]
    assert result["all_suppliers_complete"] is False
    assert any(i["kind"] == "incomplete" and "Beta" in i["message"] for i in result["insights"])


def test_a_cheaper_incomplete_supplier_does_not_win() -> None:
    """Beta's total is lower only because they left a line out.

    This is the whole reason the module exists: ranked on total alone, Beta
    looks like the winner and somebody buys eight items thinking they bought ten.
    """
    result = run(
        [
            quote("Alpha", [item("Filter", 10, 120, part="FX"), item("Seal", 4, 85, part="SK")]),
            quote("Beta", [item("Filter", 10, 105, part="FX")]),
        ]
    )

    assert result["suppliers"][1]["total"] < result["suppliers"][0]["total"]
    assert result["cheapest_supplier"]["supplier_name"] == "Alpha"


def test_with_nobody_complete_the_ranking_still_returns_something() -> None:
    """Better a ranked-but-flagged answer than an empty one."""
    result = run(
        [
            quote("Alpha", [item("Filter", 1, 100, part="FX")]),
            quote("Beta", [item("Seal", 1, 50, part="SK")]),
        ]
    )
    assert result["cheapest_supplier"] is not None
    assert result["all_suppliers_complete"] is False


# ── the split award ────────────────────────────────────────────────────


def test_the_split_award_buys_each_line_from_the_cheapest() -> None:
    result = run(
        [
            quote("Alpha", [item("Filter", 10, 120, part="FX"), item("Seal", 4, 90, part="SK")]),
            quote("Beta", [item("Filter", 10, 105, part="FX"), item("Seal", 4, 100, part="SK")]),
        ]
    )

    # Alpha 1560, Beta 1450. Split takes the filter from Beta and the seal from
    # Alpha, so the saving is measured against Beta — the cheapest single source,
    # which is the only alternative a buyer would actually have chosen.
    split = result["split_award"]
    assert split["total"] == 10 * 105 + 4 * 90 == 1410
    assert split["by_supplier"] == {"Alpha": 360.0, "Beta": 1050.0}
    assert split["saving"]["against"] == "Beta"
    assert split["saving"]["amount"] == pytest.approx(1450 - 1410)


def test_no_saving_is_reported_when_one_supplier_wins_everything() -> None:
    result = run(
        [
            quote("Alpha", [item("Filter", 1, 100, part="FX")]),
            quote("Beta", [item("Filter", 1, 150, part="FX")]),
        ]
    )
    assert result["split_award"]["saving"] is None


# ── comparing per unit, not per line ───────────────────────────────────


def test_the_cheapest_offer_is_decided_on_unit_price() -> None:
    """Suppliers quote different quantities; line totals would compare scope."""
    result = run(
        [
            quote("Alpha", [item("Filter", 100, 10, part="FX")]),  # 1000 total, 10 each
            quote("Beta", [item("Filter", 2, 90, part="FX")]),  # 180 total, 90 each
        ]
    )

    group = result["groups"][0]
    assert group["best"]["supplier_name"] == "Alpha"


def test_a_line_only_one_supplier_bid_on_is_called_out() -> None:
    result = run(
        [
            quote(
                "Alpha",
                [item("Filter", 1, 100, part="FX"), item("Rare part", 1, 900, part="RP")],
            ),
            quote("Beta", [item("Filter", 1, 105, part="FX")]),
        ]
    )
    assert any(i["kind"] == "single_source" for i in result["insights"])


def test_a_wildly_higher_price_is_called_out_as_worth_checking() -> None:
    result = run(
        [
            quote("Alpha", [item("Drier", 1, 1000, part="DR")]),
            quote("Beta", [item("Drier", 1, 2400, part="DR")]),
        ]
    )
    outliers = [i for i in result["insights"] if i["kind"] == "outlier"]
    assert outliers and "Beta" in outliers[0]["message"]


# ── currency ───────────────────────────────────────────────────────────


def test_a_foreign_quote_is_converted_before_comparison() -> None:
    result = run(
        [
            quote("Alpha", [item("Filter", 10, 100, part="FX")]),  # AED
            quote(
                "Beta",
                [item("Filter", 10, 30, part="FX")],
                currency="USD",
                fx_rate=Decimal("3.67"),
            ),
        ]
    )

    beta = result["suppliers"][1]
    assert beta["converted"] is True
    assert beta["total"] == pytest.approx(10 * 30 * 3.67)
    # 110.10 per unit converted vs 100 — Alpha is cheaper once compared properly.
    assert result["groups"][0]["best"]["supplier_name"] == "Alpha"
    assert any(i["kind"] == "converted" for i in result["insights"])


# ── the model's uncertainty reaches the reader ─────────────────────────


def test_an_extraction_note_becomes_a_warning() -> None:
    result = run(
        [quote("Alpha", [item("Filter", 1, 100)], extraction_note="Line 3 price illegible")]
    )
    notes = [i for i in result["insights"] if i["kind"] == "extraction"]
    assert notes and "illegible" in notes[0]["message"]


def test_incomparability_is_reported_before_price() -> None:
    """Order matters: a buyer reads down and must hit the caveat first."""
    result = run(
        [
            quote("Alpha", [item("Filter", 10, 120, part="FX"), item("Seal", 4, 85, part="SK")]),
            quote("Beta", [item("Filter", 10, 105, part="FX")]),
        ]
    )
    kinds = [i["kind"] for i in result["insights"]]
    assert kinds.index("incomplete") < kinds.index("split_award")


# ── reading the files ──────────────────────────────────────────────────


def test_pdfs_and_images_go_to_the_model_untouched() -> None:
    """Converting them to text first destroys the table layout that carries the prices."""
    pdf = prepare("quote.pdf", b"%PDF-1.5 ...")
    assert pdf.kind == "document" and pdf.data and pdf.text is None

    png = prepare("scan.png", b"\x89PNG...")
    assert png.kind == "image" and png.data


def test_the_extension_beats_a_vague_browser_mime_type() -> None:
    """Browsers send application/octet-stream for .xlsx more often than not."""
    assert resolve_type("prices.xlsx", "application/octet-stream").endswith("spreadsheetml.sheet")
    assert resolve_type("quote.pdf", None) == "application/pdf"


def test_a_csv_becomes_a_grid_that_keeps_its_columns() -> None:
    csv_bytes = b"Item,Qty,Price\nFilter FX-200,10,120.00\nSeal kit,4,85.00\n"
    readable = prepare("quote.csv", csv_bytes)

    assert readable.kind == "text"
    assert "Filter FX-200 | 10 | 120.00" in readable.text


def test_a_semicolon_delimited_export_is_still_read() -> None:
    readable = prepare("quote.csv", b"Item;Qty;Price\nFilter;10;120\n")
    assert "Filter | 10 | 120" in readable.text


def test_a_spreadsheet_is_read_sheet_by_sheet() -> None:
    from openpyxl import Workbook

    book = Workbook()
    book.active.title = "Prices"
    book.active.append(["Item", "Qty", "Price"])
    book.active.append(["Filter FX-200", 10, 120])
    terms = book.create_sheet("Terms")
    terms.append(["Delivery", "4 weeks"])

    buffer = io.BytesIO()
    book.save(buffer)
    readable = prepare("quote.xlsx", buffer.getvalue())

    assert "Filter FX-200 | 10 | 120" in readable.text
    # Commercial terms often live on a second sheet.
    assert "Delivery | 4 weeks" in readable.text


def test_an_unreadable_type_says_what_is_accepted() -> None:
    with pytest.raises(DocumentError, match="PDF"):
        prepare("quote.zip", b"PK\x03\x04", "application/zip")


def test_an_empty_file_is_refused() -> None:
    with pytest.raises(DocumentError, match="empty"):
        prepare("quote.pdf", b"")


def test_an_oversized_file_is_refused_before_the_api_sees_it() -> None:
    with pytest.raises(DocumentError, match="limit"):
        prepare("huge.pdf", b"x" * (21 * 1_048_576))


# ── cost: not paying image prices for text ─────────────────────────────


def _pdf(text_per_page: list[str]) -> bytes:
    """A minimal born-digital PDF with a real text layer."""
    from reportlab.pdfgen import canvas  # type: ignore

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer)
    for page in text_per_page:
        y = 800
        for line in page.splitlines():
            pdf.drawString(60, y, line)
            y -= 14
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def test_a_pdf_with_a_text_layer_is_sent_as_text() -> None:
    """A page as an image costs ~1500 tokens; as text, nearer 100."""
    pytest.importorskip("reportlab")
    body = "\n".join(
        ["Supplier: Alpha Trading LLC", "Quote QT-1042", "Filter FX-200  10  120.00"] * 6
    )
    readable = prepare("quote.pdf", _pdf([body]))

    assert readable.kind == "text"
    assert "Alpha Trading" in readable.text
    # Far smaller than the file it came from — that difference is the saving.
    assert len(readable.text) < 2000


def test_a_scan_with_no_ocr_engine_is_handed_back_as_a_document(monkeypatch) -> None:
    """No text layer, and nothing installed to read one: the file is carried as
    it is, and the extractor says to type it in."""
    from app.comparison import documents

    pytest.importorskip("reportlab")
    monkeypatch.setattr(documents, "ocr_available", lambda: False)
    readable = prepare("scan.pdf", _pdf([""]))
    assert readable.kind == "document"


def test_force_native_skips_the_text_layer(monkeypatch) -> None:
    """The escape hatch for a layout the text path mangled: the text layer is
    ignored, and without an OCR engine the file is carried as a document."""
    from app.comparison import documents

    pytest.importorskip("reportlab")
    monkeypatch.setattr(documents, "ocr_available", lambda: False)
    body = "\n".join(["Supplier: Alpha", "Filter FX-200 10 120.00"] * 8)
    assert prepare("quote.pdf", _pdf([body]), force_native=True).kind == "document"


def test_a_corrupt_pdf_falls_back_rather_than_failing() -> None:
    """Anything unreadable becomes the model's problem, not a 500."""
    assert prepare("broken.pdf", b"%PDF-1.4 not really a pdf").kind == "document"


# ── cost: not paying for a matching call that adds nothing ─────────────


def test_agreeing_part_numbers_skip_the_matching_call() -> None:
    from app.comparison.analysis import _part_numbers_settle_it

    agreed = to_domain(
        [
            quote("Alpha", [item("Filter element FX-200", 10, 120, part="FX-200")]),
            quote("Beta", [item("FX200 Filter Elm.", 10, 105, part="FX 200")]),
        ]
    )
    assert _part_numbers_settle_it(agreed) is True


def test_a_missing_part_number_needs_the_words() -> None:
    from app.comparison.analysis import _part_numbers_settle_it

    partial = to_domain(
        [
            quote("Alpha", [item("Filter element FX-200", 10, 120, part="FX-200")]),
            quote("Beta", [item("Filter, 200 series", 10, 105)]),
        ]
    )
    assert _part_numbers_settle_it(partial) is False


def test_part_numbers_that_never_overlap_need_the_words() -> None:
    """Each supplier using their own internal codes is the hard case, not the easy one."""
    from app.comparison.analysis import _part_numbers_settle_it

    private_codes = to_domain(
        [
            quote("Alpha", [item("Filter", 10, 120, part="ALPHA-001")]),
            quote("Beta", [item("Filter", 10, 105, part="BETA-777")]),
        ]
    )
    assert _part_numbers_settle_it(private_codes) is False


# ── reading a quote without AI ─────────────────────────────────────────

QUOTE_CSV = b"""Alpha Trading LLC
Quotation No: QT-1042
Date: 14-05-2025
Currency: AED
Delivery: 4 weeks ex-stock
Payment Terms: 30 days net
Validity: 30 days from date of offer
Item,Part No,Qty,Unit Price,Amount
Filter element FX-200,FX-200,10,120.00,1200.00
Seal kit,SK-9,4,85.00,340.00
Subtotal,,,,1540.00
VAT 5%,,,,77.00
Grand Total,,,,1617.00
"""


def local(content: bytes, name: str = "quote.csv"):
    from app.comparison.extraction import parse_locally

    return parse_locally(prepare(name, content))


def test_a_regular_quote_is_read_with_no_model_at_all() -> None:
    """The common case. It must not cost a call, and must not vary between runs."""
    quote = local(QUOTE_CSV)

    assert quote is not None
    assert quote.supplier_name == "Alpha Trading LLC"
    assert quote.quote_number == "QT-1042"
    assert quote.currency == "AED"
    assert quote.payment_terms == "30 days net"
    assert len(quote.items) == 2
    assert quote.items[0].unit_price == 120.0


def test_summary_rows_are_not_mistaken_for_items() -> None:
    """Subtotal, VAT and Grand Total are not things anybody is buying."""
    quote = local(QUOTE_CSV)
    assert [i.description for i in quote.items] == ["Filter element FX-200", "Seal kit"]


def test_the_tax_amount_is_taken_not_the_tax_rate() -> None:
    """'VAT 5% ... 77.00' means 77.00. Reading the 5 understates it 15-fold."""
    quote = local(QUOTE_CSV)
    assert quote.tax == 77.0
    assert quote.quoted_total == 1617.0


def test_a_supplier_is_not_read_out_of_a_terms_sentence() -> None:
    """'30 days from date of offer' once became the supplier's name."""
    quote = local(QUOTE_CSV)
    assert "date of offer" not in (quote.supplier_name or "")


def test_a_document_with_no_prices_is_declined() -> None:
    """A spec sheet is not a quote. Declining sends it to the model, correctly."""
    spec = b"IT Specification Document\nMaterial,Firewall FortiGate 201G\nAsset Type,Firewall\n"
    assert local(spec, "spec.csv") is None


def test_a_scan_is_declined_without_being_read() -> None:
    """No text layer means nothing to parse; that is the model's job."""
    from app.comparison.documents import Readable
    from app.comparison.parsing import parse

    assert parse(Readable("document", "application/pdf", "scan.pdf", data=b"%PDF")) is None


def test_a_table_the_parser_barely_understands_is_declined() -> None:
    """Half a quote read is worse than none — it looks like a complete one."""
    mostly_notes = (
        b"Item,Unit Price\n"
        + b"".join(b"Note line %d,\n" % n for n in range(9))
        + b"Filter,120.00\n"
    )
    assert local(mostly_notes) is None


def test_prices_survive_either_decimal_convention() -> None:
    from app.comparison.parsing import to_number

    assert to_number("1.234,56") == Decimal("1234.56")  # European
    assert to_number("1,234.56") == Decimal("1234.56")  # UK/US
    assert to_number("1,200") == Decimal("1200")  # thousands, not 1.2
    assert to_number("12,50") == Decimal("12.50")  # decimal comma
    assert to_number("AED 1 234.56") == Decimal("1234.56")
    assert to_number("(85.00)") == Decimal("-85.00")  # parenthesised negative
    assert to_number("abc") is None
    assert to_number("") is None


def test_a_missing_unit_price_is_derived_from_the_line_total() -> None:
    """Some quotes only print an amount. Per-unit is what comparison needs."""
    quote = local(b"Description,Qty,Amount\nFilter,10,1200.00\nSeal,4,340.00\n")
    assert quote is not None
    assert quote.items[0].unit_price == 120.0


def test_the_note_says_the_quote_was_read_locally() -> None:
    """A reviewer should know which reader produced the numbers in front of them."""
    quote = local(QUOTE_CSV)
    assert "without AI" in quote.note


def test_a_missing_supplier_or_currency_is_flagged_for_checking() -> None:
    quote = local(b"Description,Qty,Unit Price\nFilter,10,120.00\nSeal,4,85.00\n")
    assert quote is not None
    assert "currency" in quote.note and "confirm" in quote.note


# ── the model path, when the API refuses the schema ────────────────────


def test_every_extraction_field_is_required() -> None:
    """An optional field is what made the API reject this schema.

    Measured against the live API: 15 flat fields WITH defaults was rejected as
    "Schema is too complex" after 185 seconds; the same 15 required took 7. A
    field with a default is optional in JSON Schema, and structured output
    compiles the schema into a decoding grammar — every optional field multiplies
    the key orderings the grammar must admit. Required fields have exactly one.

    So a default added here is not a convenience; it is a three-minute timeout.
    """
    import json

    from app.comparison.extraction import ExtractedItem, ExtractedQuote

    for model in (ExtractedQuote, ExtractedItem):
        schema = model.model_json_schema()
        optional = set(schema["properties"]) - set(schema.get("required", []))
        assert not optional, f"{model.__name__} has optional fields: {optional}"

    whole = json.dumps(ExtractedQuote.model_json_schema())
    assert '"anyOf"' not in whole
    assert len(whole) < 6000

EMAIL_QUOTE = b"""Outlook
Re: Enquiry for Toner
From Yalla LLC <info@yallallc.com>
Date Tue 9/22/2026 12:09 PM
To Jasna <jasna@hamdaz.com>

Hello,

Hope you are doing well

Black Cartridge- 410A (CF410A)@300
Magenta Cartridge- 410A (CF413A)@370
Cyan Cartridge- 508A (CF361A) @530
Yellow Cartridge- 508A (CF362A) @670

Regards
Diya

On Tue, Sep 22, 2026 at 11:13 AM Jasna <jasna@hamdaz.com> wrote:
Dear Team,
Kindly provide the proposal for the following.
Black Cartridge- 410A (CF410A)-1 Unit
Magenta Cartridge- 410A (CF413A)-1 Unit
Cyan Cartridge- 508A (CF361A)-1 Unit
Yellow Cartridge- 508A (CF362A)-2 Unit
Tel : +971 23090211
"""


def test_a_quotation_typed_into_an_email_is_read() -> None:
    """The supplier answered the enquiry by writing a price after each item.
    No table, no quantity column: the quantities are in the enquiry quoted
    underneath, and the part numbers are in brackets."""
    from app.comparison.documents import Readable

    quote = parse(Readable("text", "text/plain", "Toner_Quote.pdf", text=EMAIL_QUOTE.decode()))

    assert quote is not None
    assert quote.supplier_name == "Yalla LLC"
    assert [(i.part_number, i.unit_price, i.quantity) for i in quote.items] == [
        ("CF410A", 300.0, 1.0),
        ("CF413A", 370.0, 1.0),
        ("CF361A", 530.0, 1.0),
        ("CF362A", 670.0, 2.0),
    ]
    assert quote.items[0].description == "Black Cartridge- 410A (CF410A)"
    # The phone number and the date are not prices.
    assert all("Tel" not in i.description and "Date" not in i.description for i in quote.items)
    assert "currency" in quote.note


def test_a_lone_colon_line_is_not_a_quote() -> None:
    """"Total: 1,200.00" on its own, or "Ref: 4471" twice, is not a quotation."""
    from app.comparison.documents import Readable

    text = "Quotation\nRef: 4471\nDate: 22/09/2026\nValidity: 30 days\n"
    assert parse(Readable("text", "text/plain", "x.pdf", text=text)) is None

# ── what the model is not trusted to decide ───────────────────────────


def _modelled(**overrides):
    from app.comparison.extraction import ExtractedItem, ExtractedQuote

    fields = dict(
        supplier_name="Redington", quote_number="", quote_date="", currency="",
        validity="", delivery_time="", payment_terms="", warranty="", incoterms="",
        contact="", discount=0, freight=0, tax=0, quoted_total=0, note="",
        items=[
            ExtractedItem(description="Pro Flex KB", part_number="Y8U-00015", brand="",
                          unit="", quantity=0, unit_price=120.5, line_total=0, lead_time=""),
            ExtractedItem(description="MS Pro CM EHS+", part_number="EP2-19400", brand="",
                          unit="", quantity=0, unit_price=0, line_total=482.0, lead_time=""),
        ],
    )
    fields.update(overrides)
    return ExtractedQuote(**fields)


def test_a_model_reading_with_no_quantities_means_one_unit_each() -> None:
    """The model follows "0 when not printed" to the letter and every line
    multiplies to nothing. A quote with no quantities is one of each."""
    from app.comparison.extraction import settle_model_reading

    quote = _modelled()
    settled = settle_model_reading(quote, "Prices in $ per unit")

    assert [i.quantity for i in quote.items] == [1, 1]
    assert quote.items[0].line_total == 120.5
    # A unit price of nothing beside a line total is the total divided out.
    assert quote.items[1].unit_price == 482.0
    assert "quantity" in settled and "unit price" in settled
    assert "one unit" in quote.note


def test_a_blank_currency_is_read_off_the_documents_symbols() -> None:
    from app.comparison.extraction import settle_model_reading

    quote = _modelled(currency="")
    settle_model_reading(quote, "Total $ 1,200.00 excl. VAT")
    assert quote.currency == "USD"
    assert "USD" in quote.note

    stated = _modelled(currency="EUR")
    settle_model_reading(stated, "Total $ 1,200.00")
    assert stated.currency == "EUR"


async def test_the_model_pass_settles_what_it_read() -> None:
    """End to end through the extractor: a stub model answers like the real
    one did — quantities 0, currency blank — and the quote comes out priced."""
    from app.comparison.documents import Readable
    from app.comparison.extraction import JSON_SHAPE, QuoteExtractor
    from app.core.config import get_settings

    class StubModel:
        configured = True

        async def extract(self, *, instructions, text, shape, max_tokens=4000):
            assert shape is JSON_SHAPE and "1 when the quote prints none" in instructions
            return (
                {
                    "supplier_name": "Redington", "quote_number": "", "quote_date": "",
                    "currency": "", "validity": "", "delivery_time": "", "payment_terms": "",
                    "warranty": "", "incoterms": "", "contact": "", "discount": 0,
                    "freight": 0, "tax": 0, "quoted_total": 0,
                    "items": [
                        {"description": "Pro Flex KB", "part_number": "Y8U-00015", "brand": "",
                         "unit": "", "quantity": 0, "unit_price": 120.5, "line_total": 0,
                         "lead_time": ""},
                    ],
                    "note": "Currency inferred from $ as USD.",
                },
                "stub:model",
            )

    extractor = QuoteExtractor(get_settings(), model=StubModel())
    readable = Readable("text", "text/plain", "redington.pdf",
                        text="RE: RFQ-9033\nPro Flex KB Y8U-00015 $120.50\n")
    quote = await extractor.read(readable)

    assert quote.currency == "USD"
    assert quote.items[0].quantity == 1
    assert quote.items[0].line_total == 120.5
    assert quote.note.startswith("Read by a model (stub:model)")
