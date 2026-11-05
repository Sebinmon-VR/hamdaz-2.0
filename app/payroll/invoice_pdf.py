"""A month's invoice as a PDF — what the report carries until Zoho's own exists.

Once an invoice is really in Zoho, the report attaches *Zoho's* PDF: that is
the document of record. Until then (test mode, or Zoho's PDF failing to
download) this one stands in, laid out like it and marked TEST so nobody
files it as the real thing.

From the India company in payroll settings (Hamdaz Technologies LLP) to the
invoice's customer (by default the UAE company, Hamdaztech Technologies LLC).
"""

from __future__ import annotations

import io
from typing import Any

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.models.payroll import PayrollInvoice, PayrollRun, PayrollSettings
from app.payroll.exports import LINE, LOGO, NAVY, ROW, _styles
from app.payroll.rules import dec

TOP = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]


def _money(value: Any) -> str:
    return f"{dec(value):,.2f}"


def _pct(value: Any) -> str:
    return f"{dec(value or 0).normalize():f} %"


def invoice_pdf(
    run: PayrollRun,
    invoice: PayrollInvoice,
    lines: list[dict[str, Any]],
    settings: PayrollSettings,
    *,
    test: bool,
) -> bytes:
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
    head.setStyle(TableStyle(TOP))
    story += [head, Spacer(1, 6 * mm)]

    def kv(label: str, value: Any) -> list[Any]:
        text = str(value) if value not in (None, "") else "—"
        return [Paragraph(label, s["muted"]), Paragraph(text, s["base"])]

    due = (
        f"{invoice.due_date:%d %b %Y}"
        if invoice.due_date
        else (f"{invoice.payment_terms} days" if invoice.payment_terms is not None else None)
    )
    facts = [
        kv("Invoice date", f"{invoice.invoice_date:%d %b %Y}"),
        kv("Due", due),
        kv("Reference", invoice.reference_number),
        kv("Currency", invoice.currency),
    ]
    bill: list[list[Any]] = [
        [Paragraph("Bill to", s["muted"])],
        [Paragraph(f"<b>{invoice.customer_name}</b>", s["base"])],
    ]
    if invoice.customer_address:
        bill.append([Paragraph(invoice.customer_address.replace("\n", "<br/>"), s["muted"])])
    top = Table(
        [[Table(bill, colWidths=[85 * mm]), Table(facts, colWidths=[25 * mm, 55 * mm])]],
        colWidths=[95 * mm, 85 * mm],
    )
    top.setStyle(TableStyle(TOP))
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
    for i, line in enumerate(lines, start=1):
        text = f"<b>{line['name']}</b>"
        if line.get("description"):
            text += f"<br/><font color='#64727f'>{line['description']}</font>"
        rows.append(
            [
                Paragraph(str(i), s["base"]),
                Paragraph(text, s["base"]),
                Paragraph(f"{dec(line['quantity']).normalize():f}", s["right"]),
                Paragraph(_money(line["rate"]), s["right"]),
                Paragraph(_pct(line.get("tax_percentage")) if line.get("tax_id") else "—", s["right"]),
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

    sub = dec(sum((dec(line["amount"]) for line in lines), dec(0)))
    sums: list[list[Any]] = [
        [Paragraph("Sub total", s["right"]), Paragraph(_money(sub), s["right"])]
    ]
    taxes: dict[str, Any] = {}
    for line in lines:
        if line.get("tax_id"):
            label = f"{line.get('tax_name') or 'Tax'} ({_pct(line.get('tax_percentage'))})"
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
    total = sub + sum((dec(v) for v in taxes.values()), dec(0)) + dec(invoice.adjustment or 0)
    sums.append(
        [
            Paragraph(f"<b>Total ({invoice.currency})</b>", s["bold_right"]),
            Paragraph(f"<b>{_money(total)}</b>", s["bold_right"]),
        ]
    )
    summary = Table(sums, colWidths=[40 * mm, 30 * mm], hAlign="RIGHT")
    summary.setStyle(
        TableStyle([("LINEABOVE", (0, -1), (-1, -1), 0.8, NAVY), ("TOPPADDING", (0, 0), (-1, -1), 2)])
    )
    story += [summary, Spacer(1, 8 * mm)]

    for label, value in (("Notes", invoice.notes), ("Terms and conditions", invoice.terms)):
        if value:
            story += [
                Paragraph(label, s["muted"]),
                Paragraph(value.replace("\n", "<br/>"), s["base"]),
                Spacer(1, 3 * mm),
            ]

    approved = (
        f"Approved by {run.approved_by.display_name} on {run.approved_at:%d %b %Y}."
        if run.approved_by and run.approved_at
        else "Not approved yet."
    )
    story += [Spacer(1, 4 * mm), Paragraph(f"Prepared in Hamdaz payroll. {approved}", s["muted"])]

    doc.build(story)
    return buffer.getvalue()
