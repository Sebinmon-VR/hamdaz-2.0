"""A payroll invoice as a PDF — what the report carries until Zoho's own exists.

Once an invoice is really in Zoho, the report attaches *Zoho's* PDF: that is
the document of record. Until then (test mode, or Zoho's PDF failing to
download) this one stands in, laid out like it and marked TEST so nobody
files it as the real thing.

The issuer is the India company in payroll settings — payroll is the side
raising the invoice; the customer is the Zoho contact chosen on it.
"""

from __future__ import annotations

import io
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.models.payroll import PayrollInvoice, PayrollSettings
from app.payroll.exports import INK, LINE, LOGO, MUTED, NAVY, ROW, _styles
from app.payroll.rules import dec

PINK = colors.HexColor("#d62d7d")


def _money(value: Any) -> str:
    return f"{dec(value):,.2f}"


def invoice_pdf(invoice: PayrollInvoice, settings: PayrollSettings, *, test: bool) -> bytes:
    s = _styles()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=14 * mm,
        bottomMargin=14 * mm,
        title=f"Invoice {invoice.number}",
        author=settings.in_company_name,
    )
    story: list[Any] = []

    issuer: list[Any] = [Paragraph(settings.in_company_name, s["org"])]
    if settings.in_company_address:
        issuer.append(Paragraph(settings.in_company_address.replace("\n", "<br/>"), s["muted"]))
    logo = Image(str(LOGO), width=14 * mm, height=14 * mm) if LOGO.exists() else ""
    title: list[Any] = [Paragraph("INVOICE", s["title"]), Paragraph(invoice.number, s["right"])]
    if test:
        title.append(
            Paragraph("<font color='#d62d7d'><b>TEST — not created in Zoho</b></font>", s["right"])
        )
    elif invoice.zoho_invoice_number:
        title.append(Paragraph(f"Zoho {invoice.zoho_invoice_number}", s["right"]))
    head = Table([[logo, issuer, title]], colWidths=[18 * mm, 100 * mm, 62 * mm])
    head.setStyle(
        TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)])
    )
    story += [head, Spacer(1, 6 * mm)]

    def kv(label: str, value: Any) -> list[Any]:
        return [
            Paragraph(label, s["muted"]),
            Paragraph(str(value) if value not in (None, "") else "—", s["base"]),
        ]

    facts = [
        kv("Invoice date", f"{invoice.invoice_date:%d %b %Y}"),
        kv(
            "Due date",
            f"{invoice.due_date:%d %b %Y}"
            if invoice.due_date
            else (f"{invoice.payment_terms} days" if invoice.payment_terms is not None else None),
        ),
        kv("Reference", invoice.reference_number),
        kv("Currency", invoice.currency),
    ]
    bill = [
        [Paragraph("Bill to", s["muted"])],
        [Paragraph(f"<b>{invoice.customer_name}</b>", s["base"])],
    ]
    top = Table(
        [[Table(bill, colWidths=[85 * mm]), Table(facts, colWidths=[25 * mm, 55 * mm])]],
        colWidths=[95 * mm, 85 * mm],
    )
    top.setStyle(
        TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)])
    )
    story += [top, Spacer(1, 6 * mm)]

    rows: list[list[Any]] = [
        [
            Paragraph("<b>#</b>", s["base"]),
            Paragraph("<b>Item and description</b>", s["base"]),
            Paragraph("<b>Qty</b>", s["right"]),
            Paragraph("<b>Rate</b>", s["right"]),
            Paragraph("<b>Tax</b>", s["right"]),
            Paragraph("<b>Amount</b>", s["right"]),
        ]
    ]
    for i, line in enumerate(invoice.lines, start=1):
        text = f"<b>{line['name']}</b>"
        if line.get("description"):
            text += f"<br/><font color='#64727f'>{line['description']}</font>"
        tax = (
            f"{dec(line.get('tax_percentage') or 0).normalize():f} %" if line.get("tax_id") else "—"
        )
        rows.append(
            [
                Paragraph(str(i), s["base"]),
                Paragraph(text, s["base"]),
                Paragraph(f"{dec(line['quantity']).normalize():f}", s["right"]),
                Paragraph(_money(line["rate"]), s["right"]),
                Paragraph(tax, s["right"]),
                Paragraph(_money(line["amount"]), s["right"]),
            ]
        )
    table = Table(
        rows, colWidths=[8 * mm, 92 * mm, 16 * mm, 24 * mm, 16 * mm, 24 * mm], repeatRows=1
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), ROW),
                ("LINEBELOW", (0, 0), (-1, -1), 0.5, LINE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story += [table, Spacer(1, 4 * mm)]

    sums: list[list[Any]] = [
        [Paragraph("Sub total", s["right"]), Paragraph(_money(invoice.sub_total), s["right"])]
    ]
    taxes: dict[str, Any] = {}
    for line in invoice.lines:
        if line.get("tax_id"):
            label = f"{line.get('tax_name') or 'Tax'} ({dec(line.get('tax_percentage') or 0).normalize():f} %)"
            taxes[label] = dec(taxes.get(label, 0)) + dec(line.get("tax_amount") or 0)
    for label, value in taxes.items():
        sums.append([Paragraph(label, s["right"]), Paragraph(_money(value), s["right"])])
    if dec(invoice.adjustment or 0) != 0:
        sums.append(
            [
                Paragraph(invoice.adjustment_description or "Adjustment", s["right"]),
                Paragraph(_money(invoice.adjustment), s["right"]),
            ]
        )
    sums.append(
        [
            Paragraph(f"<b>Total ({invoice.currency})</b>", s["bold_right"]),
            Paragraph(f"<b>{_money(invoice.total)}</b>", s["bold_right"]),
        ]
    )
    totals = Table(sums, colWidths=[40 * mm, 30 * mm], hAlign="RIGHT")
    totals.setStyle(
        TableStyle(
            [
                ("LINEABOVE", (0, -1), (-1, -1), 0.8, NAVY),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )
    story += [totals, Spacer(1, 8 * mm)]

    for label, value in (("Notes", invoice.notes), ("Terms and conditions", invoice.terms)):
        if value:
            story += [
                Paragraph(label, s["muted"]),
                Paragraph(value.replace("\n", "<br/>"), s["base"]),
                Spacer(1, 3 * mm),
            ]

    approved = (
        f"Approved by {invoice.approved_by.display_name} on {invoice.approved_at:%d %b %Y}."
        if invoice.approved_by and invoice.approved_at
        else "Not approved yet."
    )
    story += [Spacer(1, 4 * mm), Paragraph(f"Prepared in Hamdaz payroll. {approved}", s["muted"])]

    doc.build(story)
    return buffer.getvalue()


__all__ = ["invoice_pdf", "INK", "MUTED", "PINK"]
