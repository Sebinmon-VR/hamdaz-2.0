"""Payroll's mails: an invoice to approve, and an approved invoice's report.

Sent through Graph as the person who pressed the button, so a reply goes to
them. Both are behind ``PayrollSettings.invoice_mail`` (off by default) — see
``app/payroll/invoicing.py``, which decides whether to call these at all.
"""

from __future__ import annotations

from typing import Any

from app.core.mail import Attachment, GraphMailer
from app.models.payroll import PayrollInvoice
from app.payroll.invoicing import summary_html


def _button(link: str, label: str) -> str:
    return (
        f"<p><a href='{link}' style='display:inline-block;padding:8px 14px;background:#0e5e80;"
        f"color:#fff;text-decoration:none;border-radius:6px;font-family:Segoe UI,Arial,sans-serif'>"
        f"{label}</a></p>"
    )


class PayrollMailer(GraphMailer):
    async def invoice_for_approval(
        self, invoice: PayrollInvoice, *, sender: str, recipients: list[str], link: str
    ) -> dict[str, Any]:
        who = invoice.submitted_by.display_name if invoice.submitted_by else "Payroll"
        html = (
            "<div style='font-family:Segoe UI,Arial,sans-serif;font-size:14px'>"
            f"<p>{who} has prepared an invoice and sent it to you to check and approve.</p>"
            f"{summary_html(invoice)}"
            f"{_button(link, 'Open the invoice to approve')}"
            "<p style='color:#64727f;font-size:12px'>Once approved it is created in Zoho Books and the "
            "report goes to the super admins, the CEO and the Accounts team.</p></div>"
        )
        return await self.send(
            sender=sender,
            recipients=recipients,
            subject=f"Invoice to approve — {invoice.number} — {invoice.customer_name} — "
            f"{invoice.currency} {invoice.total:,.2f}",
            html=html,
        )

    async def invoice_report(
        self, invoice: PayrollInvoice, *, sender: str, recipients: list[str], link: str, pdf: bytes
    ) -> dict[str, Any]:
        live = invoice.zoho_mode == "live"
        where = (
            f"It is in Zoho Books as <b>{invoice.zoho_invoice_number}</b>."
            if live
            else "<b>Test mode:</b> it was checked against Zoho Books but not created there."
        )
        approved = f"Approved by {invoice.approved_by.display_name}." if invoice.approved_by else ""
        html = (
            "<div style='font-family:Segoe UI,Arial,sans-serif;font-size:14px'>"
            f"<p>Invoice {invoice.number} to {invoice.customer_name} has been approved. {approved} {where}</p>"
            f"{summary_html(invoice)}"
            f"{_button(link, 'Open the invoice')}</div>"
        )
        name = f"{invoice.zoho_invoice_number or invoice.number}{'' if live else ' TEST'}.pdf"
        return await self.send(
            sender=sender,
            recipients=recipients,
            subject=f"{'' if live else '[TEST] '}Invoice {invoice.zoho_invoice_number or invoice.number} — "
            f"{invoice.customer_name} — {invoice.currency} {invoice.total:,.2f}",
            html=html,
            attachments=[Attachment(name=name, content=pdf, content_type="application/pdf")],
        )
