"""The bid pack as the workbook it was modelled on.

Presales built a costing workbook by hand for these bids long before any of this
existed, and its five sheets are what the bid pack's tables were shaped from.
This turns the stored bid back into that workbook — same sheets, same order,
same columns, same yellow-for-input convention — so that what leaves this system
is a document the people receiving it already know how to read.

It matters that this is a *rendering* and not a second source of truth. Every
figure here comes from ``app.quoting.bidpack``, the same computation the screen
draws from, so an exported workbook and the page it came from cannot disagree.
Nothing is recalculated in this file; the numbers are formatted, not derived.

The one deliberate departure from the original: the last sheet is called
"Portal Fields" rather than "Ariba Response Fields". The stored data is
portal-agnostic — a buyer on a different system gets the same columns — and
labelling every bid as Ariba would be wrong for most of them.

Formulas are not written. The cells carry values, because a workbook whose
totals are formulas over cells this file laid out would break the first time a
row was added, and a broken formula in a bid document is worse than a number.
"""

from __future__ import annotations

import io
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from app.models.quoting import QuoteRequest
from app.quoting import bidpack
from app.quoting.bidpack import BidPack

#: What a spreadsheet is, to anything that has to decide how to open it —
#: a mail client, a browser download, a drive upload.
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

ORG = "Hamdaztech Technology Services LLC"

#: The workbook's own convention, stated on its Landed Cost sheet: yellow cells
#: are the ones a person fills in, everything else follows from them.
YELLOW = PatternFill("solid", fgColor="FFF2CC")
BAND = PatternFill("solid", fgColor="E7E6E6")
HEAD = PatternFill("solid", fgColor="D9D9D9")
TOTAL = PatternFill("solid", fgColor="F2F2F2")
RED = PatternFill("solid", fgColor="FCE4E4")
AMBER = PatternFill("solid", fgColor="FDF1DC")
GREEN = PatternFill("solid", fgColor="E3F3E9")

THIN = Side(style="thin", color="BFBFBF")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

MONEY = "#,##0.00"
QTY = "#,##0.####"
PCT = '0.0"%"'

TITLE_FONT = Font(bold=True, size=13)
ORG_FONT = Font(bold=True, size=9, color="808080")
BAND_FONT = Font(bold=True, size=10)
HEAD_FONT = Font(bold=True, size=9)
WRAP = Alignment(wrap_text=True, vertical="top")
WRAP_MID = Alignment(wrap_text=True, vertical="center")
RIGHT = Alignment(horizontal="right", vertical="center")


def _clean(value: Any) -> Any:
    """A value Excel will take.

    Decimals go through as floats — openpyxl will not write a ``Decimal`` — and
    that is safe here and nowhere else in this module's neighbourhood: this is
    the last step before a display grid, after every sum has been done in exact
    arithmetic. Dates lose their time, which they never meaningfully had.
    """
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.date()
    return value


def _title(ws: Worksheet, row: int, width: int, title: str, subtitle: str = "") -> int:
    """The heading block every sheet of the original opens with."""
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=width)
    cell = ws.cell(row=row, column=1, value=ORG)
    cell.font = ORG_FONT
    cell.alignment = Alignment(horizontal="center")

    ws.merge_cells(start_row=row + 1, start_column=1, end_row=row + 1, end_column=width)
    cell = ws.cell(row=row + 1, column=1, value=title.upper())
    cell.font = TITLE_FONT
    cell.alignment = Alignment(horizontal="center")

    if subtitle:
        ws.merge_cells(
            start_row=row + 2, start_column=1, end_row=row + 2, end_column=width
        )
        cell = ws.cell(row=row + 2, column=1, value=subtitle)
        cell.font = Font(size=9, color="595959")
        cell.alignment = Alignment(horizontal="center")
        return row + 4
    return row + 3


def _band(ws: Worksheet, row: int, width: int, text: str, fill: PatternFill = BAND) -> int:
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=width)
    cell = ws.cell(row=row, column=1, value=text.upper())
    cell.font = BAND_FONT
    cell.fill = fill
    cell.border = BOX
    return row + 1


def _fact(
    ws: Worksheet,
    row: int,
    label: str,
    value: Any,
    *,
    width: int,
    note: str = "",
    editable: bool = False,
    number_format: str | None = None,
    bold: bool = False,
) -> int:
    """One label/value row, with the original's right-hand commentary column."""
    key = ws.cell(row=row, column=1, value=label)
    key.font = Font(bold=bold, size=10)
    key.border = BOX
    key.alignment = WRAP_MID

    # The value spans to the column before the note, so a long verdict has room
    # without the note column moving about between rows.
    last = width - 1 if note or width > 2 else width
    if last > 2:
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=last)
    cell = ws.cell(row=row, column=2, value=_clean(value))
    cell.font = Font(bold=bold, size=10)
    cell.border = BOX
    cell.alignment = WRAP_MID
    if editable:
        cell.fill = YELLOW
    if number_format:
        cell.number_format = number_format
        cell.alignment = RIGHT

    if note:
        n = ws.cell(row=row, column=width, value=note)
        n.font = Font(size=8, color="808080")
        n.border = BOX
        n.alignment = WRAP
    elif width > 2:
        ws.cell(row=row, column=width).border = BOX
    return row + 1


def _header_row(ws: Worksheet, row: int, headings: list[str]) -> int:
    for index, text in enumerate(headings, start=1):
        cell = ws.cell(row=row, column=index, value=text)
        cell.font = HEAD_FONT
        cell.fill = HEAD
        cell.border = BOX
        cell.alignment = WRAP_MID
    return row + 1


def _row(
    ws: Worksheet,
    row: int,
    values: list[Any],
    *,
    formats: dict[int, str] | None = None,
    fill: PatternFill | None = None,
    bold: bool = False,
    editable: set[int] | None = None,
) -> int:
    for index, value in enumerate(values, start=1):
        cell = ws.cell(row=row, column=index, value=_clean(value))
        cell.border = BOX
        cell.alignment = WRAP
        cell.font = Font(bold=bold, size=10)
        if fill:
            cell.fill = fill
        if editable and index in editable and not fill:
            cell.fill = YELLOW
        if formats and index in formats:
            cell.number_format = formats[index]
            cell.alignment = Alignment(horizontal="right", vertical="top")
    return row + 1


def _widths(ws: Worksheet, widths: list[int]) -> None:
    for index, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(index)].width = width


def _note(ws: Worksheet, row: int, width: int, text: str) -> int:
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=width)
    cell = ws.cell(row=row, column=1, value=text)
    cell.font = Font(size=8, italic=True, color="808080")
    cell.alignment = WRAP
    return row + 1


# ── the five sheets ────────────────────────────────────────────────────


def _summary(ws: Worksheet, request: QuoteRequest, pack: BidPack) -> None:
    width = 4
    _widths(ws, [34, 42, 28, 46])
    currency = request.currency or "AED"

    subtitle = " · ".join(
        part
        for part in (
            request.rfp_number or None,
            request.line_item_ref or None,
            f"Pass {request.revision}" if request.revision > 1 else None,
            f"Closes {request.cf_bcd:%d %b %Y}" if request.cf_bcd else None,
        )
        if part
    )
    row = _title(ws, 1, width, "Bid summary — compliance & costing", subtitle)

    row = _band(ws, row, width, "1. Event particulars")
    facts: list[tuple[str, Any, str]] = [
        ("Event / RFP no.", request.rfp_number, ""),
        ("Buying entity", request.buying_entity, ""),
        ("Line item", request.line_item_ref, ""),
        ("Customer", request.customer_name, ""),
        (
            "Quantity",
            pack.landed.quantity,
            pack.landed.per_unit_note or "",
        ),
        ("Class / manufacturer no.", request.manufacturer_class_no, ""),
        ("Manufacturer", request.manufacturer_name, ""),
        ("Manufacturer part no.", request.manufacturer_part_number, ""),
        (
            "Incoterm required",
            " ".join(
                p for p in (request.incoterm_required, request.incoterm_place) if p
            ),
            "The gap between this and the supplier's own term is the freight, duty and documentation.",
        ),
        ("Ship to, as stated", request.ship_to, ""),
        ("Requested delivery date", request.requested_delivery_date, ""),
        ("Bid currency", currency, ""),
        ("Mode of shipment", request.mode_of_shipment, ""),
        (
            "Bid validity",
            f"{request.bid_validity_days} days" if request.bid_validity_days else "",
            "",
        ),
    ]
    for label, value, note in facts:
        row = _fact(ws, row, label, value, width=width, note=note, editable=True)

    row += 1
    row = _band(ws, row, width, "2. Recommended bid position")
    row = _fact(
        ws, row, "Technical verdict", request.technical_verdict, width=width, editable=True
    )
    row = _fact(
        ws, row, "Commercial verdict", request.commercial_verdict, width=width, editable=True
    )
    row = _fact(
        ws,
        row,
        f"Total landed cost ({currency})",
        pack.landed.total,
        width=width,
        number_format=MONEY,
        note=f"{pack.landed.firm_percent}% of it committed; the rest is our estimate.",
    )
    if pack.landed.per_unit is not None:
        row = _fact(
            ws,
            row,
            f"Landed cost per unit ({currency})",
            pack.landed.per_unit,
            width=width,
            number_format=MONEY,
        )
    row = _fact(
        ws,
        row,
        "Recommended markup",
        request.target_markup_percent,
        width=width,
        number_format=PCT,
        editable=True,
        note="On landed cost. Not the same number as the margin.",
    )
    row = _fact(
        ws,
        row,
        f"RECOMMENDED BID PRICE ({currency})",
        pack.bid_total,
        width=width,
        number_format=MONEY,
        bold=True,
        note=(
            "Suggested by the markup above — no price has been decided."
            if pack.bid_total_is_suggested
            else f"Margin {pack.gross_margin_percent}% on the landed cost, before tax."
        ),
    )
    if pack.bid_unit_price is not None:
        row = _fact(
            ws,
            row,
            f"Unit price ({currency})",
            pack.bid_unit_price,
            width=width,
            number_format=MONEY,
            bold=True,
        )
    row = _fact(
        ws,
        row,
        "Delivery to declare",
        f"{request.delivery_days} calendar days from order"
        if request.delivery_days
        else "",
        width=width,
        editable=True,
    )
    row = _fact(
        ws,
        row,
        "Country of origin",
        request.country_of_origin,
        width=width,
        editable=True,
        note="Where the goods are made — never defaulted to our own country.",
    )

    row += 1
    row = _band(
        ws,
        row,
        width,
        "3. Clear before submission",
        RED if any(not f.resolved for f in pack.red_flags) else BAND,
    )
    if not pack.red_flags:
        row = _note(ws, row, width, "Nothing flagged on the compliance matrix.")
    else:
        row = _header_row(ws, row, ["Severity", "Issue", "Action required", "Owner"])
        for flag in pack.red_flags:
            fill = (
                GREEN
                if flag.resolved
                else RED
                if str(flag.severity) in ("stopper", "critical")
                else AMBER
            )
            row = _row(
                ws,
                row,
                [
                    "Cleared" if flag.resolved else str(flag.severity).upper(),
                    f"{flag.ref + ' — ' if flag.ref else ''}{flag.issue}",
                    flag.action,
                    flag.owner,
                ],
                fill=fill,
            )


def _compliance(ws: Worksheet, request: QuoteRequest) -> None:
    width = 8
    _widths(ws, [7, 40, 12, 40, 15, 11, 40, 14])
    row = _title(
        ws,
        1,
        width,
        "Compliance matrix",
        "Green is compliant. Amber is a deviation — priceable or curable. "
        "Red must be cured or formally declared before submission.",
    )

    areas = [
        ("technical", "Technical — specification"),
        ("commercial", "Commercial — delivery, terms and price"),
        ("documents", "Commercial — bid package documents"),
        ("logistics", "Logistics & customs"),
    ]
    rows = sorted(request.compliance, key=lambda c: c.position or 0)
    for key, heading in areas:
        in_area = [c for c in rows if str(c.area) == key]
        if not in_area:
            continue
        row = _band(ws, row, width, heading)
        row = _header_row(
            ws,
            row,
            [
                "Ref",
                "RFP requirement",
                "Clause",
                "Supplier's position",
                "Status",
                "Urgency",
                "Action required",
                "Owner",
            ],
        )
        for item in in_area:
            status = str(item.status)
            fill = (
                GREEN
                if item.resolved_at or status == "compliant"
                else RED
                if status in ("non_compliant", "risk")
                else AMBER
                if status in ("deviation", "open", "clarify")
                else None
            )
            row = _row(
                ws,
                row,
                [
                    item.ref,
                    item.requirement,
                    item.source_clause,
                    item.supplier_position,
                    "CLEARED" if item.resolved_at else status.replace("_", " ").upper(),
                    str(item.severity).upper() if item.severity else "",
                    item.action,
                    item.owner,
                ],
                fill=fill,
            )
        row += 1

    if not rows:
        _note(ws, row, width, "No compliance matrix has been built for this quote.")


def _landed(ws: Worksheet, request: QuoteRequest, pack: BidPack) -> None:
    width = 6
    _widths(ws, [6, 40, 30, 16, 16, 38])
    currency = request.currency or "AED"
    foreign = request.supplier_currency or ""
    landed = pack.landed

    row = _title(
        ws,
        1,
        width,
        "Landed cost build-up",
        " ".join(
            p
            for p in (
                request.incoterm_required or "",
                "to",
                request.incoterm_place or "the delivery point",
            )
            if p
        ),
    )
    row = _note(
        ws,
        row,
        width,
        "Yellow cells are inputs. Every other figure is worked out from them — "
        "the goods come from the quote's own priced lines, and duty and financing "
        "are arithmetic on the rows above them.",
    )
    row += 1

    row = _band(ws, row, width, "Input assumptions")
    row = _fact(ws, row, "Supplier currency", foreign, width=width, editable=True)
    row = _fact(
        ws,
        row,
        f"{currency} per {foreign or 'unit'}",
        request.fx_rate,
        width=width,
        editable=True,
        note="The rate the bid is costed at — mid-market plus a spread.",
    )
    row = _fact(
        ws,
        row,
        "Import duty rate",
        request.customs_duty_percent,
        width=width,
        number_format=PCT,
        editable=True,
        note="Charged on the value at arrival, freight included.",
    )
    row = _fact(
        ws,
        row,
        "Cost of money, a year",
        request.financing_rate_percent,
        width=width,
        number_format=PCT,
        editable=True,
    )
    row = _fact(
        ws,
        row,
        "Days our money is out",
        request.cash_exposure_days,
        width=width,
        editable=True,
    )
    row = _fact(
        ws,
        row,
        "Markup on landed cost",
        request.target_markup_percent,
        width=width,
        number_format=PCT,
        editable=True,
    )
    row += 1

    row = _band(ws, row, width, "Cost elements")
    row = _header_row(
        ws,
        row,
        ["#", "Cost element", "Basis / source", foreign or "Quoted", currency, "Notes"],
    )

    seen_destination = False
    for element in landed.elements:
        if str(element.stage) == "destination" and not seen_destination:
            seen_destination = True
            row = _row(
                ws,
                row,
                ["", "Value on arrival — the duty base", "", "", landed.cif_subtotal, ""],
                formats={5: MONEY},
                fill=TOTAL,
                bold=True,
            )
        row = _row(
            ws,
            row,
            [
                element.ref,
                element.label + ("" if not element.computed else "  (worked out)"),
                element.basis,
                element.amount_source,
                element.amount_base,
                element.notes,
            ],
            formats={4: MONEY, 5: MONEY},
            editable=None if element.computed else {4, 5},
        )

    row = _row(
        ws,
        row,
        ["", f"TOTAL LANDED COST ({currency})", "", "", landed.total, "Excl. tax"],
        formats={5: MONEY},
        fill=TOTAL,
        bold=True,
    )
    if landed.per_unit is not None:
        row = _row(
            ws,
            row,
            [
                "",
                f"Landed cost per unit, over {landed.quantity}",
                "",
                "",
                landed.per_unit,
                "",
            ],
            formats={5: MONEY},
            fill=TOTAL,
        )
    row += 1
    _note(
        ws,
        row,
        width,
        f"{landed.firm_percent}% of this cost is committed — quoted by the supplier or "
        f"the forwarder. The rest is our estimate, and every point of it that comes in "
        f"high comes out of the margin rather than the price.",
    )


def _costing(ws: Worksheet, request: QuoteRequest, pack: BidPack) -> None:
    width = 7
    _widths(ws, [10, 46, 9, 10, 16, 16, 18])
    currency = request.currency or "AED"

    row = _title(
        ws,
        1,
        width,
        "Costing sheet",
        f"All figures in {currency}, exclusive of tax",
    )

    row = _header_row(
        ws,
        row,
        ["Line", "Description", "UoM", "Qty", "Unit cost", "Unit sell", "Total sell"],
    )
    for index, item in enumerate(request.items, start=1):
        row = _row(
            ws,
            row,
            [
                request.line_item_ref or index,
                item.name,
                item.unit,
                item.quantity,
                item.cost_rate,
                item.rate,
                item.line_total,
            ],
            formats={4: QTY, 5: MONEY, 6: MONEY, 7: MONEY},
        )
    if not request.items:
        row = _note(ws, row, width, "Nothing priced yet.")

    for label, value in (
        ("Total landed cost", pack.landed.total),
        ("Gross margin", pack.gross_margin),
    ):
        row = _row(
            ws, row, ["", label, "", "", "", "", value], formats={7: MONEY},
            fill=TOTAL, bold=True,
        )
    row = _row(
        ws,
        row,
        [
            "", "Gross margin, on the landed cost, before tax", "", "", "", "",
            pack.gross_margin_percent,
        ],
        formats={7: PCT},
        fill=TOTAL,
    )
    row += 1

    row = _band(ws, row, width, "Recommended submission price")
    row = _fact(
        ws,
        row,
        f"Unit price to enter ({currency})",
        pack.bid_unit_price,
        width=width,
        number_format=MONEY,
        editable=True,
        bold=True,
    )
    row = _fact(
        ws,
        row,
        f"Total bid value ({currency})",
        pack.bid_total,
        width=width,
        number_format=MONEY,
        bold=True,
        note="Suggested by the markup — no price decided."
        if pack.bid_total_is_suggested
        else "",
    )
    row += 1

    row = _band(ws, row, width, "Markup sensitivity")
    row = _header_row(
        ws, row, ["Markup", "Unit sell", "Total sell", "Margin", "", "", "Comment"]
    )
    for scenario in pack.scenarios:
        row = _row(
            ws,
            row,
            [
                scenario.markup_percent,
                scenario.unit_sell,
                scenario.total_sell,
                scenario.margin_percent,
                "",
                "",
                "THIS BID" if scenario.is_target else "",
            ],
            formats={1: PCT, 2: MONEY, 3: MONEY, 4: PCT},
            fill=TOTAL if scenario.is_target else None,
            bold=scenario.is_target,
        )
    row += 1

    disclosure = pack.disclosure
    row = _band(
        ws,
        row,
        width,
        "Price-disclosure exposure",
        RED if disclosure.disclosed else BAND,
    )
    if not disclosure.disclosed:
        _note(
            ws,
            row,
            width,
            "The supplier's own quotation is not a mandatory attachment on this bid, "
            "so the buyer does not see what we paid.",
        )
        return

    for label, value, fmt in (
        (f"What the supplier charged us ({currency})", disclosure.principal_value, MONEY),
        (f"What we are bidding ({currency})", disclosure.bid_value, MONEY),
        ("Apparent uplift", disclosure.apparent_uplift_percent, PCT),
        (f"Of which recoverable cost ({currency})", disclosure.recoverable_cost, MONEY),
        ("True margin retained", disclosure.true_margin_percent, PCT),
    ):
        row = _fact(ws, row, label, value, width=width, number_format=fmt)
    _note(
        ws,
        row,
        width,
        "The buyer will see the supplier's quotation alongside ours. Most of the "
        "apparent uplift is freight, duty, documentation and the cost of paying the "
        "supplier before anybody pays us — so the landed-cost build-up must go in as "
        "the price breakdown. Cleaner still: ask the supplier to re-quote on delivered "
        "terms, so those costs sit inside their price and never surface as ours.",
    )


def _portal(ws: Worksheet, request: QuoteRequest) -> None:
    width = 6
    _widths(ws, [10, 34, 10, 46, 40, 10])
    row = _title(
        ws,
        1,
        width,
        "Portal fields",
        "Values to enter before the bid is uploaded back to the buyer's system.",
    )
    rows = sorted(request.submission_fields, key=lambda f: f.position or 0)
    if not rows:
        _note(ws, row, width, "No portal checklist has been built for this quote.")
        return

    row = _header_row(
        ws, row, ["Clause", "Field", "Cell", "Value to enter", "Note", "Entered"]
    )
    for field in rows:
        missing = field.is_mandatory and not (field.value or "").strip()
        row = _row(
            ws,
            row,
            [
                field.clause,
                field.label + (" *" if field.is_mandatory else ""),
                field.destination,
                field.value,
                field.note,
                "Yes" if field.entered_at else "",
            ],
            fill=GREEN if field.entered_at else RED if missing else None,
            editable={4},
        )
    row += 1
    _note(
        ws,
        row,
        width,
        "* mandatory — the portal will not accept the bid without it.",
    )


# ── the whole thing ────────────────────────────────────────────────────


def filename_for(request: QuoteRequest) -> str:
    """A name that says which bid this is without being opened.

    Built from the buyer's event number where there is one, because that is what
    a bid is filed and searched by everywhere else.
    """
    stem = (
        request.rfp_number
        or request.reference
        or request.bid_reference
        or request.title
        or "quote"
    )
    safe = "".join(c if c.isalnum() or c in "-_ " else "_" for c in stem).strip()
    safe = "_".join(safe.split())[:60] or "quote"
    stamp = date.today().isoformat()
    revision = f"_rev{request.revision}" if request.revision > 1 else ""
    return f"Bid_{safe}{revision}_{stamp}.xlsx"


def build(request: QuoteRequest) -> bytes:
    """The bid pack as an .xlsx, in the shape presales already works in."""
    pack = bidpack.build(request)

    book = Workbook()
    # openpyxl opens with one sheet already made; it becomes the first of ours
    # rather than being deleted and re-added, which would put it last.
    summary = book.active
    summary.title = "Summary"
    _summary(summary, request, pack)

    _compliance(book.create_sheet("Compliance Matrix"), request)
    _landed(book.create_sheet("Landed Cost"), request, pack)
    _costing(book.create_sheet("Costing Sheet"), request, pack)
    _portal(book.create_sheet("Portal Fields"), request)

    for sheet in book.worksheets:
        sheet.sheet_view.showGridLines = False
        sheet.freeze_panes = "A2"

    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()
