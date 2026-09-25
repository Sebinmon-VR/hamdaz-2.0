"""The selling & costing report as a PDF.

A rendering of ``report.CostingReport`` and nothing else: the numbers are
formatted here, never derived, so the PDF an approver is mailed and the
report on screen cannot disagree.

The page is the one presales already knows — Hamdaz letterhead top left,
the title top right, the customer and the supplier side by side, four figures
in tiles, then the four numbered sections and a line to sign. Built with
platypus rather than drawn at coordinates so a quote with forty lines flows
onto a second page under the same letterhead instead of running off the
bottom of the first.

``reportlab`` is already a dependency (the reports module exports with it),
pure Python, and behaves the same on Azure as on a laptop. The fonts are the
built-in Helvetica family for the same reason: nothing to install.
"""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    Flowable,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.models.quoting import QuoteRequest
from app.quoting import report as report_mod
from app.quoting.report import (
    ACCEPTABLE,
    COMFORTABLE,
    LOSS,
    CostingReport,
    Figure,
)

PDF_TYPE: Final = "application/pdf"

ASSETS: Final = Path(__file__).with_name("assets")
LOGO: Final = ASSETS / "logo_mark.png"
FOOTER: Final = ASSETS / "footer.png"

ORG: Final = "HAMDAZTECH TECHNOLOGY SERVICES - L.L.C"
ADDRESS: Final = (
    "PO Box 5758, Office 22 | 2nd Floor, Millennium Tower",
    "Opp. Hamdan Centre, Hamdan Street, Abu Dhabi, U.A.E",
    "E-mail: hello@hamdaz.com | Phone: +971 2 626 5780",
    "TRN: 104219757200003",
)

# The brand, as the statement of accounts already uses it.
NAVY = colors.HexColor("#0e5e80")
NAVY_DEEP = colors.HexColor("#0b4d69")
CYAN = colors.HexColor("#46bcec")
MAGENTA = colors.HexColor("#ed4995")
MAGENTA_TEXT = colors.HexColor("#d62d7d")
INK = colors.HexColor("#22303f")
MUTED = colors.HexColor("#64727f")
FAINT = colors.HexColor("#8593a1")
LINE = colors.HexColor("#e2e9ef")
BOX = colors.HexColor("#dde5ec")
ROW = colors.HexColor("#fafcfd")
ROW_BLUE = colors.HexColor("#eef8fd")
TILE_BLUE = colors.HexColor("#f7fbfd")
TILE_PINK = colors.HexColor("#fdf5f9")
TILE_GREY = colors.HexColor("#cbd6e0")
PANEL = colors.HexColor("#f7fbfd")
AMBER = colors.HexColor("#fdf1dc")
AMBER_TEXT = colors.HexColor("#8a5a00")

PAGE_W, PAGE_H = A4
MARGIN: Final = 31.0
WIDTH: Final = PAGE_W - 2 * MARGIN
TOP: Final = 108.0  # where the body starts, below the letterhead
BOTTOM: Final = 70.0  # room for the certification strip

_STYLES: dict[str, ParagraphStyle] = {}


def _style(name: str, **kw: Any) -> ParagraphStyle:
    if name not in _STYLES:
        base = {"fontName": "Helvetica", "fontSize": 8.5, "leading": 10.5, "textColor": INK}
        base.update(kw)
        _STYLES[name] = ParagraphStyle(name, **base)
    return _STYLES[name]


def _esc(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def money(value: Decimal | None) -> str:
    return "—" if value is None else f"{value:,.2f}"


def pct(value: Decimal | None, places: int = 1) -> str:
    return "—" if value is None else f"{value:.{places}f}%"


def _both(figure: Figure | None, currency: str, base: str) -> str:
    """"USD 3,988.94 / AED 14,649.38", or one of them."""
    if figure is None:
        return "—"
    text = f"{currency} {money(figure.amount)}"
    if figure.base is not None:
        text += f" / {base} {money(figure.base)}"
    return text


def _day(value: date | None) -> str:
    return value.strftime("%d %b %Y") if value else "—"


# ── the letterhead and the strip, on every page ────────────────────────


def _page(report: CostingReport):
    logo = ImageReader(str(LOGO)) if LOGO.exists() else None
    footer = ImageReader(str(FOOTER)) if FOOTER.exists() else None

    def draw(canvas, doc) -> None:
        canvas.saveState()
        y = PAGE_H

        # Logo, and the address block beside it.
        if logo is not None:
            canvas.drawImage(logo, MARGIN, y - 82, width=69, height=54, mask="auto")
            canvas.setFont("Helvetica", 6.6)
            canvas.setFillColor(FAINT)
            canvas.drawString(MARGIN + 3, y - 93, "w w w . h a m d a z . c o m")
        canvas.setFont("Helvetica-Bold", 9.4)
        canvas.setFillColor(NAVY)
        canvas.drawString(118, y - 41, ORG)
        canvas.setFont("Helvetica", 8.2)
        canvas.setFillColor(MUTED)
        for i, line in enumerate(ADDRESS):
            canvas.drawString(118, y - 56 - i * 12.3, line)

        # The title, and the rule and the particulars under it.
        canvas.setFont("Helvetica-Bold", 14)
        canvas.setFillColor(NAVY)
        canvas.drawRightString(PAGE_W - MARGIN, y - 45, "SELLING & COSTING REPORT")
        rule_w, rule_h = 181.0, 2.4
        canvas.setFillColor(CYAN)
        canvas.rect(PAGE_W - MARGIN - rule_w, y - 53, rule_w * 0.62, rule_h, stroke=0, fill=1)
        canvas.setFillColor(MAGENTA)
        canvas.rect(
            PAGE_W - MARGIN - rule_w * 0.38, y - 53, rule_w * 0.38, rule_h, stroke=0, fill=1
        )
        canvas.setFont("Helvetica", 8.4)
        canvas.setFillColor(MUTED)
        currencies = report.currency
        if report.base_rate is not None:
            currencies = (
                f"{report.currency} & {report.base_currency} "
                f"(1 {report.currency} = {report_mod._trim(report.base_rate)} "
                f"{report.base_currency})"
            )
        meta = f"Quote {report.reference}  |  {currencies}"
        canvas.drawRightString(PAGE_W - MARGIN, y - 68, meta)
        canvas.setFont("Helvetica-Bold", 8.4)
        canvas.setFillColor(INK)
        canvas.drawRightString(PAGE_W - MARGIN, y - 82, f"Prepared {_day(report.prepared_on)}")
        if doc.page > 1:
            canvas.setFont("Helvetica", 7.5)
            canvas.setFillColor(FAINT)
            canvas.drawRightString(PAGE_W - MARGIN, y - 94, f"continued · page {doc.page}")

        canvas.setStrokeColor(LINE)
        canvas.setLineWidth(0.8)
        canvas.line(MARGIN, y - 100, PAGE_W - MARGIN, y - 100)

        # The certification strip.
        if footer is not None:
            iw, ih = footer.getSize()
            fh = WIDTH * ih / iw
            canvas.drawImage(footer, MARGIN, 17, width=WIDTH, height=fh, mask="auto")
            canvas.setFont("Helvetica", 6.5)
            canvas.setFillColor(FAINT)
            canvas.drawString(
                MARGIN,
                17 + fh + 4,
                f"Selling & costing report  ·  {report.reference}  ·  {report.customer.name}",
            )
            canvas.drawRightString(PAGE_W - MARGIN, 17 + fh + 4, f"Page {doc.page}")
        canvas.restoreState()

    return draw


# ── small flowables ────────────────────────────────────────────────────


class SectionTitle(Flowable):
    """"1. SELLING VS COSTING BY LINE" with a hairline running to the edge."""

    def __init__(self, text: str, width: float, size: float = 8.6) -> None:
        super().__init__()
        self.text = text.upper()
        self.width = width
        self.size = size

    def wrap(self, *_: Any) -> tuple[float, float]:
        return self.width, self.size + 6

    def draw(self) -> None:
        c = self.canv
        # A text object rather than drawString: letter-spacing lives on the
        # text object, and the headings are set a little open, as the page
        # presales knows has them.
        text = c.beginText(0, 3)
        text.setFont("Helvetica-Bold", self.size)
        text.setFillColor(NAVY)
        text.setCharSpace(0.9)
        text.textOut(self.text)
        c.drawText(text)
        text_w = c.stringWidth(self.text, "Helvetica-Bold", self.size) + len(self.text) * 0.9
        c.setStrokeColor(LINE)
        c.setLineWidth(0.6)
        c.line(text_w + 8, 6, self.width, 6)


class Pill(Flowable):
    """A rounded status label — Healthy margin, Acceptable, Below walk-away, Loss."""

    def __init__(self, text: str, background, foreground) -> None:
        super().__init__()
        self.text = text
        self.background = background
        self.foreground = foreground
        self._w = 0.0

    def wrap(self, *_: Any) -> tuple[float, float]:
        from reportlab.pdfbase.pdfmetrics import stringWidth

        self._w = stringWidth(self.text, "Helvetica-Bold", 7.2) + 14
        return self._w, 11

    def draw(self) -> None:
        c = self.canv
        c.setFillColor(self.background)
        c.roundRect(0, 0, self._w, 11, 5.5, stroke=0, fill=1)
        c.setFillColor(self.foreground)
        c.setFont("Helvetica-Bold", 7.2)
        c.drawCentredString(self._w / 2, 3, self.text)


LOSS_RED = colors.HexColor("#fde2e1")
LOSS_TEXT = colors.HexColor("#a32d2d")


def _status_pill(status: str) -> Pill:
    if status == COMFORTABLE:
        return Pill("Healthy margin", CYAN, colors.white)
    if status == ACCEPTABLE:
        return Pill("Acceptable", ROW_BLUE, NAVY)
    if status == LOSS:
        return Pill("Loss", LOSS_RED, LOSS_TEXT)
    return Pill("Below walk-away", AMBER, AMBER_TEXT)


# ── the blocks ─────────────────────────────────────────────────────────


def _party_box(heading: str, name: str, lines: list[str], width: float) -> Table:
    head = _style("party_head", fontName="Helvetica-Bold", fontSize=7.4, leading=9, textColor=CYAN)
    strong = _style("party_name", fontName="Helvetica-Bold", fontSize=8.4, leading=11)
    body = _style("party_body", fontSize=8.4, leading=11, textColor=MUTED)
    content = [
        Paragraph(_esc(heading).upper(), head),
        Paragraph(_esc(name), strong),
        *[Paragraph(_esc(line), body) for line in lines if line],
    ]
    table = Table([[content]], colWidths=[width])
    table.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.6, BOX),
                ("BACKGROUND", (0, 0), (-1, -1), colors.white),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    return table


def _parties(report: CostingReport) -> Table:
    c, s = report.customer, report.supplier
    customer_name = c.name + (f"  ·  Portal ref {c.reference}" if c.reference else "")
    customer_lines = []
    if c.end_user:
        customer_lines.append(f"End user: {c.end_user}")
    bits = []
    if c.place_of_supply:
        bits.append(f"Place of supply: {c.place_of_supply}")
    if c.valid_from or c.valid_until:
        bits.append(f"Valid: {_day(c.valid_from)} – {_day(c.valid_until)}")
    if bits:
        customer_lines.append("  ·  ".join(bits))
    if c.portal:
        customer_lines.append(f"Portal: {c.portal}")

    supplier_name = s.name or "Supplier not stated"
    if s.basis:
        supplier_name += f"  ({s.basis})"
    supplier_bits = []
    if s.route:
        supplier_bits.append(f"Route: {s.route}")
    if s.creator:
        supplier_bits.append(f"Creator: {s.creator}")
    supplier_lines = ["  ·  ".join(supplier_bits)] if supplier_bits else []
    if s.quote_number:
        supplier_lines.append(f"Their ref: {s.quote_number}")

    half = (WIDTH - 6) / 2
    row = [
        _party_box("Customer", customer_name, customer_lines, half),
        "",
        _party_box("Supplier", supplier_name, supplier_lines, half),
    ]
    table = Table([row], colWidths=[half, 6, half])
    table.setStyle(
        TableStyle(
            [
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    return table


def _tile(label: str, figure: Figure, report: CostingReport, top, bg, value_colour) -> Table:
    lab = _style("tile_label", fontName="Helvetica-Bold", fontSize=7, leading=9, textColor=FAINT)
    big = _style(
        f"tile_value_{value_colour.hexval()}",
        fontName="Helvetica-Bold",
        fontSize=13.5,
        leading=16,
        textColor=value_colour,
    )
    small = _style("tile_base", fontName="Helvetica-Bold", fontSize=9, leading=11, textColor=INK)
    content = [
        Paragraph(_esc(label).upper(), lab),
        Paragraph(f"{report.currency} {money(figure.amount)}", big),
    ]
    if figure.base is not None:
        content.append(Paragraph(f"{report.base_currency} {money(figure.base)}", small))
    table = Table([[content]], colWidths=[(WIDTH - 3 * 6) / 4])
    table.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.6, BOX),
                ("LINEABOVE", (0, 0), (-1, 0), 2.2, top),
                ("BACKGROUND", (0, 0), (-1, -1), bg),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    return table


def _tiles(report: CostingReport) -> Table:
    margin_label = "Gross margin"
    if report.gross_margin_percent is not None:
        margin_label += f"  ·  {pct(report.gross_margin_percent)}"
    tiles = [
        _tile("Quoted price (ex-VAT)", report.quoted_price, report, NAVY, TILE_BLUE, NAVY),
        _tile("Total landed cost", report.landed_total, report, TILE_GREY, colors.white, NAVY),
        _tile(margin_label, report.gross_margin, report, CYAN, TILE_BLUE, NAVY),
        _tile(
            f"Walk-away ({pct(report.walk_away_margin_percent, 0)} margin)",
            report.walk_away_price,
            report,
            MAGENTA,
            TILE_PINK,
            MAGENTA_TEXT,
        ),
    ]
    width = (WIDTH - 3 * 6) / 4
    table = Table([[tiles[0], "", tiles[1], "", tiles[2], "", tiles[3]]],
                  colWidths=[width, 6, width, 6, width, 6, width])
    table.setStyle(
        TableStyle(
            [
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    return table


# ── the tables ─────────────────────────────────────────────────────────

_HEAD = TableStyle(
    [
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 7.4),
        ("LEADING", (0, 0), (-1, 0), 9),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3.2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.2),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("FONTSIZE", (0, 1), (-1, -1), 8.5),
        ("LEADING", (0, 1), (-1, -1), 10.5),
        ("TEXTCOLOR", (0, 1), (-1, -1), INK),
        ("LINEBELOW", (0, 1), (-1, -1), 0.5, LINE),
    ]
)


def _cell(text: str, *, bold: bool = False, align: str = "left", colour=INK, size: float = 7.8):
    style = _style(
        f"cell_{bold}_{align}_{colour.hexval()}_{size}",
        fontName="Helvetica-Bold" if bold else "Helvetica",
        fontSize=size,
        leading=size + 2,
        textColor=colour,
        alignment={"left": TA_LEFT, "right": TA_RIGHT, "center": TA_CENTER}[align],
    )
    return Paragraph(text, style)


def _head(text: str, align: str = "left") -> Paragraph:
    return _cell(_esc(text).upper(), bold=True, align=align, colour=colors.white, size=6.8)


def _lines_table(report: CostingReport) -> Table:
    cur, base = report.currency, report.base_currency
    two = report.base_rate is not None
    sup = report.supplier_currency or cur

    # Column widths, from the page presales already knows. Without a second
    # currency the money columns widen to take up the room.
    widths = (
        [12, 128, 24, 44, 42, 44, 42, 46, 42, 44, 36] if two else [14, 262, 28, 57, 57, 57, 57]
    )
    scale = WIDTH / sum(widths)
    widths = [w * scale for w in widths]

    if two:
        head_1 = [
            _head("#"), _head("Part no. / description"), _head("Qty", "right"),
            _head(f"Supplier {sup}", "right"),
            _head("Landed cost", "center"), "",
            _head("Selling ex-VAT", "center"), "",
            _head("Gross margin", "center"), "",
            _head("Margin", "right"),
        ]
        head_2 = [
            "", "", "", "",
            _head(cur, "right"), _head(base, "right"),
            _head(cur, "right"), _head(base, "right"),
            _head(cur, "right"), _head(base, "right"),
            "",
        ]
        rows: list[list[Any]] = [head_1, head_2]
    else:
        rows = [[
            _head("#"), _head("Part no. / description"), _head("Qty", "right"),
            _head(f"Supplier {sup}", "right"), _head(f"Landed {cur}", "right"),
            _head(f"Selling ex-VAT {cur}", "right"), _head(f"Margin {cur}", "right"),
            _head("Margin", "right"),
        ][: len(widths)]]
        # Eight headings into seven columns: margin amount and margin percent
        # share the last two, so drop the plain "Margin" heading.
        rows[0] = [
            _head("#"), _head("Part no. / description"), _head("Qty", "right"),
            _head(f"Supplier {sup}", "right"), _head(f"Landed {cur}", "right"),
            _head(f"Selling {cur}", "right"), _head("Margin", "right"),
        ]

    for line in report.lines:
        desc = ""
        if line.part_number:
            desc += f"<b>{_esc(line.part_number)}</b>  "
        desc += f"<font color='#{MUTED.hexval()[2:]}'>{_esc(line.description)}</font>"
        qty = report_mod._trim(line.quantity)
        landed = line.landed
        margin = line.margin
        if two:
            rows.append([
                _cell(str(line.position), colour=FAINT),
                _cell(desc),
                _cell(qty, align="right"),
                _cell(money(line.supplier_amount), align="right"),
                _cell(money(landed.amount) if landed else "—", align="right"),
                _cell(money(landed.base) if landed else "—", align="right"),
                _cell(money(line.selling.amount), align="right"),
                _cell(money(line.selling.base), align="right"),
                _cell(money(margin.amount) if margin else "—", align="right", bold=True),
                _cell(money(margin.base) if margin else "—", align="right"),
                _cell(pct(line.margin_percent), align="right", bold=True),
            ])
        else:
            rows.append([
                _cell(str(line.position), colour=FAINT),
                _cell(desc),
                _cell(qty, align="right"),
                _cell(money(line.supplier_amount), align="right"),
                _cell(money(landed.amount) if landed else "—", align="right"),
                _cell(money(line.selling.amount), align="right"),
                _cell(
                    (money(margin.amount) if margin else "—") + "  " + pct(line.margin_percent),
                    align="right",
                    bold=True,
                ),
            ])

    total_cells = {
        "qty": report_mod._trim(report.total_quantity),
        "sup": money(report.total_supplier_amount),
    }
    white = colors.white
    if two:
        rows.append([
            "", _cell("Total (ex-VAT)", bold=True, colour=white),
            _cell(total_cells["qty"], bold=True, align="right", colour=white),
            _cell(total_cells["sup"], bold=True, align="right", colour=white),
            _cell(money(report.landed_total.amount), bold=True, align="right", colour=white),
            _cell(money(report.landed_total.base), bold=True, align="right", colour=white),
            _cell(money(report.quoted_price.amount), bold=True, align="right", colour=white),
            _cell(money(report.quoted_price.base), bold=True, align="right", colour=white),
            _cell(money(report.gross_margin.amount), bold=True, align="right", colour=white),
            _cell(money(report.gross_margin.base), bold=True, align="right", colour=white),
            _cell(pct(report.gross_margin_percent), bold=True, align="right", colour=white),
        ])
    else:
        rows.append([
            "", _cell("Total (ex-VAT)", bold=True, colour=white),
            _cell(total_cells["qty"], bold=True, align="right", colour=white),
            _cell(total_cells["sup"], bold=True, align="right", colour=white),
            _cell(money(report.landed_total.amount), bold=True, align="right", colour=white),
            _cell(money(report.quoted_price.amount), bold=True, align="right", colour=white),
            _cell(
                money(report.gross_margin.amount) + "  " + pct(report.gross_margin_percent),
                bold=True, align="right", colour=white,
            ),
        ])

    header_rows = 2 if two else 1
    table = Table(rows, colWidths=widths, repeatRows=header_rows)
    style = [
        ("BACKGROUND", (0, 0), (-1, header_rows - 1), NAVY),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 2.2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.2),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("LINEBELOW", (0, header_rows), (-1, -2), 0.5, LINE),
        ("BACKGROUND", (0, -1), (-1, -1), NAVY),
    ]
    if two:
        style += [
            ("BACKGROUND", (4, 0), (9, 0), NAVY_DEEP),
            ("SPAN", (0, 0), (0, 1)), ("SPAN", (1, 0), (1, 1)), ("SPAN", (2, 0), (2, 1)),
            ("SPAN", (3, 0), (3, 1)), ("SPAN", (10, 0), (10, 1)),
            ("SPAN", (4, 0), (5, 0)), ("SPAN", (6, 0), (7, 0)), ("SPAN", (8, 0), (9, 0)),
        ]
    for i in range(header_rows, len(rows) - 1):
        if (i - header_rows) % 2 == 1:
            style.append(("BACKGROUND", (0, i), (-1, i), ROW))
    table.setStyle(TableStyle(style))
    return table


def _simple_table(
    rows: list[list[Any]], widths: list[float], *, header_rows: int = 1, total_rows: int = 1
) -> Table:
    table = Table(rows, colWidths=widths, repeatRows=header_rows)
    style = [
        ("BACKGROUND", (0, 0), (-1, header_rows - 1), NAVY),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 2.2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.2),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("LINEBELOW", (0, header_rows), (-1, -1 - total_rows), 0.5, LINE),
    ]
    if total_rows:
        style.append(("BACKGROUND", (0, -total_rows), (-1, -1), NAVY))
    for i in range(header_rows, len(rows) - total_rows):
        if (i - header_rows) % 2 == 1:
            style.append(("BACKGROUND", (0, i), (-1, i), ROW))
    table.setStyle(TableStyle(style))
    return table


def _landed_table(report: CostingReport, width: float) -> Table:
    cur, base = report.currency, report.base_currency
    two = report.base_rate is not None
    widths = [width - 100, 50, 50] if two else [width - 70, 70]
    head = [_head("Cost element"), _head(cur, "right")] + ([_head(base, "right")] if two else [])
    rows: list[list[Any]] = [head]
    for row in report.cost_rows:
        label = _esc(row.label) + ("*" if row.is_estimate else "")
        cells = [_cell(label), _cell(money(row.amount.amount), align="right")]
        if two:
            cells.append(_cell(money(row.amount.base), align="right"))
        rows.append(cells)
    white = colors.white
    total = [
        _cell("Total landed cost", bold=True, colour=white),
        _cell(money(report.landed_total.amount), bold=True, align="right", colour=white),
    ]
    if two:
        total.append(_cell(money(report.landed_total.base), bold=True, align="right", colour=white))
    rows.append(total)
    return _simple_table(rows, widths)


def _value_table(report: CostingReport, width: float) -> Table:
    cur, base = report.currency, report.base_currency
    two = report.base_rate is not None
    widths = [width - 142, 67, 75] if two else [width - 80, 80]
    white = colors.white

    def row(label: str, figure: Figure, *, total: bool = False) -> list[Any]:
        colour = white if total else INK
        cells = [
            _cell(_esc(label), bold=total, colour=colour),
            _cell(money(figure.amount), bold=total, align="right", colour=colour),
        ]
        if two:
            cells.append(_cell(money(figure.base), bold=total, align="right", colour=colour))
        return cells

    rows: list[list[Any]] = [
        [_head(report.reference), _head(cur, "right")] + ([_head(base, "right")] if two else []),
        row("Sub total (ex-VAT)", report.sub_total),
        row(report.tax_label, report.tax_total),
        row("Total incl. VAT", report.total_incl_tax, total=True),
    ]
    return _simple_table(rows, widths)


def _walk_away_table(report: CostingReport, width: float) -> Table:
    cur, base = report.currency, report.base_currency
    two = report.base_rate is not None
    widths = [width - 179, 55, 56, 68] if two else [width - 140, 70, 70]
    head = [_head("Min. margin"), _head(cur, "right")]
    if two:
        head.append(_head(base, "right"))
    head.append(_head("Max. disc.", "right"))
    rows: list[list[Any]] = [head]
    for rung in report.walk_away_ladder:
        is_floor = rung.margin_percent == report.walk_away_margin_percent
        cells = [
            _cell(pct(rung.margin_percent), bold=True, colour=MAGENTA_TEXT if is_floor else INK),
            _cell(money(rung.price.amount), align="right", bold=is_floor),
        ]
        if two:
            cells.append(_cell(money(rung.price.base), align="right", bold=is_floor))
        cells.append(_cell(pct(rung.max_discount_percent), align="right", bold=True))
        rows.append(cells)
    return _simple_table(rows, widths, total_rows=0)


def _negotiation_table(report: CostingReport) -> Table:
    cur, base = report.currency, report.base_currency
    two = report.base_rate is not None
    widths = [75, 66, 73, 66, 65, 78, 109] if two else [90, 120, 120, 90, 112]
    scale = WIDTH / sum(widths)
    widths = [w * scale for w in widths]

    if two:
        rows: list[list[Any]] = [
            [
                _head("Discount", "center"), _head("Total incl. VAT", "center"), "",
                _head("Gross margin", "center"), "", _head("Margin %", "center"),
                _head("Status", "center"),
            ],
            [
                "", _head(cur, "right"), _head(base, "right"),
                _head(cur, "right"), _head(base, "right"), "", "",
            ],
        ]
    else:
        rows = [[
            _head("Discount", "center"), _head(f"Total incl. VAT {cur}", "right"),
            _head(f"Gross margin {cur}", "right"), _head("Margin %", "center"),
            _head("Status", "center"),
        ]]
    header_rows = len(rows)

    for step in report.negotiation:
        label = "Quoted" if step.discount_percent == 0 else pct(step.discount_percent)
        pill = _status_pill(step.status)
        if two:
            rows.append([
                _cell(label, bold=True, align="center"),
                _cell(money(step.total_incl_tax.amount), align="right"),
                _cell(money(step.total_incl_tax.base), align="right"),
                _cell(money(step.margin.amount), align="right", bold=True),
                _cell(money(step.margin.base), align="right"),
                _cell(pct(step.margin_percent), align="center", bold=True),
                pill,
            ])
        else:
            rows.append([
                _cell(label, bold=True, align="center"),
                _cell(money(step.total_incl_tax.amount), align="right"),
                _cell(money(step.margin.amount), align="right", bold=True),
                _cell(pct(step.margin_percent), align="center", bold=True),
                pill,
            ])

    table = Table(rows, colWidths=widths, repeatRows=header_rows)
    style = [
        ("BACKGROUND", (0, 0), (-1, header_rows - 1), NAVY),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (-1, header_rows), (-1, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 2.0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.0),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("LINEBELOW", (0, header_rows), (-1, -1), 0.5, LINE),
    ]
    if two:
        style += [
            ("BACKGROUND", (1, 0), (4, 0), NAVY_DEEP),
            ("SPAN", (0, 0), (0, 1)), ("SPAN", (5, 0), (5, 1)), ("SPAN", (6, 0), (6, 1)),
            ("SPAN", (1, 0), (2, 0)), ("SPAN", (3, 0), (4, 0)),
        ]
    for i in range(header_rows, len(rows)):
        if (i - header_rows) % 2 == 1:
            style.append(("BACKGROUND", (0, i), (-1, i), ROW))
    table.setStyle(TableStyle(style))
    return table


def _recommendation(report: CostingReport) -> Table:
    strong = _style("rec_strong", fontName="Helvetica-Bold", fontSize=8, leading=10.8)
    body = _style("rec_body", fontSize=7.8, leading=10.8, textColor=MUTED)
    content: list[Any] = [
        Paragraph(
            f"<b><font color='#{INK.hexval()[2:]}'>Recommendation:</font></b> "
            f"{_esc(report.recommendation)}",
            strong,
        )
    ]
    for note in report.notes:
        content.append(Paragraph(f"•  {_esc(note)}", body))
    for warning in report.warnings:
        content.append(
            Paragraph(f"•  <font color='#{MAGENTA_TEXT.hexval()[2:]}'>{_esc(warning)}</font>", body)
        )
    table = Table([[content]], colWidths=[WIDTH])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), PANEL),
                ("LINEBEFORE", (0, 0), (0, -1), 2, CYAN),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    return table


def _status_legend(report: CostingReport) -> Paragraph:
    """What the four statuses mean, with this quote's own thresholds in it."""
    legend = _style("legend", fontSize=6.5, leading=8, textColor=FAINT, spaceBefore=2)
    walk = f"{report.walk_away_margin_percent:.0f}%"
    comfortable = f"{report.comfortable_margin_percent:.0f}%"
    return Paragraph(
        f"<b>Healthy margin</b> at or above the comfortable margin ({comfortable}) &middot; "
        f"<b>Acceptable</b> between the walk-away ({walk}) and the comfortable margin &middot; "
        f"<b>Below walk-away</b> still positive but under the walk-away, so not without "
        f"management approval &middot; <b>Loss</b> the price no longer covers the landed cost.",
        legend,
    )


def _signatures(report: CostingReport) -> Table:
    """Three signature lines, with the names the trail already knows: who
    raised the quote, who last decided on it, who approved it. A line whose
    name is not known yet stays blank for a pen."""
    label = _style("sig", fontSize=7.8, leading=10, textColor=FAINT)
    name = _style("sig_name", fontName="Helvetica-Bold", fontSize=8.6, leading=11, textColor=NAVY)
    third = (WIDTH - 2 * 24) / 3

    def cell(who: str | None) -> Any:
        return Paragraph(who, name) if who else ""

    table = Table(
        [
            [Paragraph("Prepared by", label), "", Paragraph("Reviewed by", label), "",
             Paragraph("Approved by", label)],
            [cell(report.prepared_by), "", cell(report.reviewed_by), "", cell(report.approved_by)],
        ],
        colWidths=[third, 24, third, 24, third],
        rowHeights=[None, 13],
    )
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LINEABOVE", (0, 1), (0, 1), 0.6, TILE_GREY),
                ("LINEABOVE", (2, 1), (2, 1), 0.6, TILE_GREY),
                ("LINEABOVE", (4, 1), (4, 1), 0.6, TILE_GREY),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 2),
                ("TOPPADDING", (0, 1), (-1, 1), 3),
            ]
        )
    )
    return table


# ── the whole page ─────────────────────────────────────────────────────


def filename_for(request: QuoteRequest) -> str:
    stem = report_mod.reference_of(request)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in stem).strip("_") or "quote"
    return f"{safe}_Selling_and_Costing_Report.pdf"


def render(report: CostingReport) -> bytes:
    """The report as A4 pages."""
    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=TOP,
        bottomMargin=BOTTOM,
        title=f"Selling & costing report — {report.reference}",
        author=report.prepared_by or "Hamdaz",
        subject=report.title,
    )

    half = (WIDTH - 7) / 2
    story: list[Any] = [
        _parties(report),
        Spacer(1, 6),
        _tiles(report),
        Spacer(1, 6),
        SectionTitle("1. Selling vs costing by line", WIDTH),
        _lines_table(report),
        Spacer(1, 6),
    ]

    left = [
        SectionTitle("2. Landed cost to UAE", half),
        _landed_table(report, half),
    ]
    right = [
        SectionTitle("3. Quote value", half),
        _value_table(report, half),
        Spacer(1, 6),
        SectionTitle("Walk-away prices (ex-VAT)", half, size=7.6),
        _walk_away_table(report, half),
    ]
    side = Table([[left, "", right]], colWidths=[half, 7, half])
    side.setStyle(
        TableStyle(
            [
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    story += [side, Spacer(1, 6)]

    who = report.customer.end_user or report.customer.name
    story += [
        KeepTogether([
            SectionTitle(f"4. Negotiation — if {who} requests a discount", WIDTH),
            _negotiation_table(report),
            _status_legend(report),
        ]),
        Spacer(1, 4),
        _recommendation(report),
        Spacer(1, 6),
        _signatures(report),
    ]

    page = _page(report)
    document.build(story, onFirstPage=page, onLaterPages=page)
    return buffer.getvalue()


def build(request: QuoteRequest, **kw: Any) -> bytes:
    """The report for a quote, straight to bytes. ``kw`` is passed to
    :func:`report.build` — the base rate, chiefly."""
    return render(report_mod.build(request, **kw))
