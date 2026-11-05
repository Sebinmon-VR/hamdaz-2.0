"""Payroll's mails: a month to approve, and an approved month's report.

Sent through Graph as the person who pressed the button, so a reply goes to
them. Both sit behind ``PayrollSettings.mail_enabled`` (off by default) — see
``app/payroll/invoicing.py``, which decides whether to call these at all.
"""

from __future__ import annotations

from typing import Any

from app.core.mail import Attachment, GraphMailer
from app.models.payroll import PayrollInvoice, PayrollRun
from app.payroll.invoicing import summary_html
from app.payroll.service import _country, period_of

FONT = "font-family:Segoe UI,Arial,sans-serif;font-size:14px"


def _button(link: str, label: str) -> str:
    return (
        f"<p><a href='{link}' style='display:inline-block;padding:8px 14px;background:#0e5e80;"
        f"color:#fff;text-decoration:none;border-radius:6px;font-family:Segoe UI,Arial,sans-serif'>"
        f"{label}</a></p>"
    )


def _month(run: PayrollRun) -> str:
    return f"{_country(run.country)} payroll {period_of(run).label}"


class PayrollMailer(GraphMailer):
    async def month_for_approval(
        self,
        run: PayrollRun,
        invoice: PayrollInvoice,
        lines: list[dict[str, Any]],
        *,
        sender: str,
        recipients: list[str],
        link: str,
    ) -> dict[str, Any]:
        who = run.submitted_by.display_name if run.submitted_by else "Payroll"
        html = (
            f"<div style='{FONT}'>"
            f"<p>{who} has prepared the {_month(run)} and its invoice, and sent them to you "
            "to check and approve.</p>"
            f"{summary_html(run, invoice, lines)}"
            f"{_button(link, 'Open the month to approve')}"
            "<p style='color:#64727f;font-size:12px'>Approving approves the payroll and the "
            "invoice together. The invoice then goes to Zoho Books and the report to the super "
            "admins, the CEO and the Accounts team.</p></div>"
        )
        return await self.send(
            sender=sender,
            recipients=recipients,
            subject=f"To approve — {_month(run)} — invoice {invoice.number}, "
            f"{invoice.currency} {invoice.total:,.2f}",
            html=html,
        )

    async def month_report(
        self,
        run: PayrollRun,
        invoice: PayrollInvoice,
        lines: list[dict[str, Any]],
        *,
        sender: str,
        recipients: list[str],
        link: str,
        attachments: list[Attachment],
    ) -> dict[str, Any]:
        live = invoice.zoho_mode == "live"
        where = (
            f"The invoice is in Zoho Books as <b>{invoice.zoho_invoice_number}</b>."
            if live
            else "<b>Test mode:</b> the invoice was checked against Zoho Books but not created there."
        )
        approved = f"Approved by {run.approved_by.display_name}." if run.approved_by else ""
        html = (
            f"<div style='{FONT}'>"
            f"<p>The {_month(run)} has been approved. {approved} {where}</p>"
            f"{summary_html(run, invoice, lines)}"
            "<p>The invoice and the salary register are attached.</p>"
            f"{_button(link, 'Open the month')}</div>"
        )
        tag = "" if live else "[TEST] "
        return await self.send(
            sender=sender,
            recipients=recipients,
            subject=f"{tag}{_month(run)} — invoice {invoice.zoho_invoice_number or invoice.number}, "
            f"{invoice.currency} {invoice.total:,.2f}",
            html=html,
            attachments=attachments,
        )
