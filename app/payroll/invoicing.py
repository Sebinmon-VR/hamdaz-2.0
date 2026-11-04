"""Invoices raised from payroll, and their way into Zoho Books.

The order is the user's (2026-11-04):

1. The accountant prepares the invoice here — the customer picked from Zoho,
   lines for the month's payroll, rent, anything else.
2. It is sent for approval, and the **super admins get a mail** to check it.
3. A super admin approves it (or sends it back with a note).
4. **"Create in Zoho"** — the invoice is created in Zoho Books and Zoho's own
   PDF is fetched.
5. **The report** — that PDF, mailed to the super admins, the CEO and the
   Accounts team.

Step 4 is **test mode** unless ``PAYROLL_ZOHO_INVOICE_WRITE`` is on, and it
ships off: Zoho is the live production books, and the user asked for a button
that does not write ("as of now we dont need the real zoho"). In test mode the
invoice is still checked against Zoho — the customer, every tax and every item
are read back to make sure they exist — and the exact body Zoho would receive
is kept on the invoice. Nothing is written. The report then carries this
app's own PDF, marked TEST.

Mail in steps 2 and 5 sits behind ``PayrollSettings.invoice_mail``, also off,
so a trial invoice does not reach the CEO. With it off each step records who
it would have mailed.

The arithmetic (`clean_lines`, `totals`) and the Zoho body (`zoho_payload`)
are pure functions, so they are tested without a database or a Zoho.
"""

from __future__ import annotations

import html
import re
import uuid
from collections.abc import Iterable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.payroll import InvoiceStatus, PayrollInvoice, PayrollRun, PayrollSettings
from app.models.user import User
from app.payroll import access as payroll_access
from app.payroll.rules import dec, r2, to_json
from app.payroll.service import (
    PayrollConflict,
    PayrollError,
    PayrollNotFound,
    get_settings,
    log,
)

ZERO = Decimal("0")

# ── the arithmetic ────────────────────────────────────────────────────


def clean_lines(raw: Iterable[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Each line checked and its amount and tax worked out, the way Zoho does.

    ``amount`` is quantity × rate to the cent; ``tax_amount`` is that amount ×
    the tax rate, to the cent, per line — Zoho rounds per line too, so the
    totals agree with the invoice Zoho shows.
    """
    out: list[dict[str, Any]] = []
    for index, item in enumerate(raw or [], start=1):
        name = str(item.get("name") or "").strip()
        if not name:
            raise PayrollError(f"Line {index} needs a name.")
        try:
            quantity = dec(item.get("quantity") if item.get("quantity") not in (None, "") else 1)
            rate = dec(item.get("rate") if item.get("rate") not in (None, "") else 0)
            pct = dec(item.get("tax_percentage") or 0)
        except Exception as exc:  # noqa: BLE001
            raise PayrollError(f"Line {index} ({name}): a number is not a number.") from exc
        if quantity <= 0:
            raise PayrollError(f"Line {index} ({name}): the quantity must be more than 0.")
        if rate < 0:
            raise PayrollError(f"Line {index} ({name}): the rate cannot be negative.")
        if pct < 0 or pct > 100:
            raise PayrollError(f"Line {index} ({name}): the tax rate must be 0 to 100 %.")
        amount = r2(quantity * rate)
        tax_id = str(item.get("tax_id") or "").strip() or None
        out.append(
            {
                "item_id": str(item.get("item_id") or "").strip() or None,
                "name": name[:200],
                "description": (str(item.get("description") or "").strip() or None),
                "quantity": quantity.normalize()
                if quantity == quantity.to_integral()
                else quantity,
                "rate": r2(rate),
                "tax_id": tax_id,
                "tax_name": (str(item.get("tax_name") or "").strip() or None) if tax_id else None,
                "tax_percentage": pct if tax_id else ZERO,
                "amount": amount,
                "tax_amount": r2(amount * pct / 100) if tax_id else ZERO,
                "run_id": str(item.get("run_id")) if item.get("run_id") else None,
            }
        )
    if not out:
        raise PayrollError("An invoice needs at least one line.")
    return to_json(out)


def totals(lines: list[dict[str, Any]], adjustment: Any = 0) -> tuple[Decimal, Decimal, Decimal]:
    """(sub total, tax, total). The adjustment is added after tax, as in Zoho."""
    sub = r2(sum((dec(line["amount"]) for line in lines), ZERO))
    tax = r2(sum((dec(line["tax_amount"]) for line in lines), ZERO))
    return sub, tax, r2(sub + tax + dec(adjustment or 0))


def zoho_payload(invoice: PayrollInvoice) -> dict[str, Any]:
    """The body for Zoho's ``POST /invoices`` — exactly what would be sent.

    Zoho numbers its invoices itself, so our number goes in as the reference
    when there is no other. Numbers go as JSON numbers, which is what Zoho's
    API documents; every value here has at most two decimal places already.
    """

    def num(value: Any) -> float:
        return float(dec(value))

    body: dict[str, Any] = {
        "customer_id": invoice.customer_zoho_id,
        "reference_number": invoice.reference_number or invoice.number,
        "date": invoice.invoice_date.isoformat(),
        "line_items": [],
    }
    if invoice.due_date:
        body["due_date"] = invoice.due_date.isoformat()
    elif invoice.payment_terms is not None:
        body["payment_terms"] = invoice.payment_terms
    if invoice.exchange_rate:
        body["exchange_rate"] = num(invoice.exchange_rate)
    for line in invoice.lines:
        item: dict[str, Any] = {
            "name": line["name"],
            "quantity": num(line["quantity"]),
            "rate": num(line["rate"]),
        }
        if line.get("item_id"):
            item["item_id"] = line["item_id"]
        if line.get("description"):
            item["description"] = line["description"]
        if line.get("tax_id"):
            item["tax_id"] = line["tax_id"]
        body["line_items"].append(item)
    if dec(invoice.adjustment or 0) != 0:
        body["adjustment"] = num(invoice.adjustment)
        if invoice.adjustment_description:
            body["adjustment_description"] = invoice.adjustment_description
    if invoice.notes:
        body["notes"] = invoice.notes
    if invoice.terms:
        body["terms"] = invoice.terms
    return body


# ── reading ───────────────────────────────────────────────────────────


async def list_invoices(session: AsyncSession) -> list[PayrollInvoice]:
    rows = await session.scalars(
        select(PayrollInvoice).order_by(
            PayrollInvoice.invoice_date.desc(), PayrollInvoice.number.desc()
        )
    )
    return list(rows.unique().all())


async def get_invoice(session: AsyncSession, invoice_id: uuid.UUID) -> PayrollInvoice:
    invoice = await session.get(PayrollInvoice, invoice_id)
    if invoice is None:
        raise PayrollNotFound("No such invoice.")
    return invoice


async def next_number(session: AsyncSession, prefix: str, year: int) -> str:
    """``HZ-INV-2026-004`` — one more than the highest this year."""
    stem = f"{prefix}{year}-"
    rows = await session.scalars(
        select(PayrollInvoice.number).where(PayrollInvoice.number.like(f"{stem}%"))
    )
    highest = 0
    for number in rows.all():
        match = re.fullmatch(re.escape(stem) + r"(\d+)", number)
        if match:
            highest = max(highest, int(match.group(1)))
    return f"{stem}{highest + 1:03d}"


# ── preparing ─────────────────────────────────────────────────────────

_FIELDS = (
    "customer_zoho_id",
    "customer_name",
    "currency",
    "exchange_rate",
    "invoice_date",
    "due_date",
    "payment_terms",
    "reference_number",
    "adjustment",
    "adjustment_description",
    "notes",
    "terms",
    "run_id",
)


def _require(invoice: PayrollInvoice, *allowed: InvoiceStatus, doing: str) -> None:
    if invoice.status not in allowed:
        raise PayrollConflict(f"This invoice is {invoice.status} and cannot be {doing}.")


async def _apply(session: AsyncSession, invoice: PayrollInvoice, data: dict[str, Any]) -> None:
    for key in _FIELDS:
        if key in data:
            value = data[key]
            if isinstance(value, str):
                value = value.strip() or None
            setattr(invoice, key, value)
    if not invoice.customer_name:
        raise PayrollError("Choose the customer.")
    invoice.currency = (invoice.currency or "").upper()
    if not re.fullmatch(r"[A-Z]{3}", invoice.currency):
        raise PayrollError("The currency is a three-letter code, like AED or INR.")
    if invoice.due_date and invoice.due_date < invoice.invoice_date:
        raise PayrollError("The due date is before the invoice date.")
    if invoice.run_id and await session.get(PayrollRun, invoice.run_id) is None:
        raise PayrollError("That payroll month no longer exists.")
    if "lines" in data:
        invoice.lines = clean_lines(data["lines"])
    invoice.adjustment = r2(invoice.adjustment or 0)
    invoice.sub_total, invoice.tax_total, invoice.total = totals(invoice.lines, invoice.adjustment)


async def create_invoice(
    session: AsyncSession, data: dict[str, Any], actor: User
) -> PayrollInvoice:
    settings = await get_settings(session)
    day = data.get("invoice_date") or date.today()
    invoice = PayrollInvoice(
        number=await next_number(session, settings.invoice_prefix, day.year),
        status=InvoiceStatus.DRAFT,
        invoice_date=day,
        currency=data.get("currency") or "",
        customer_name=data.get("customer_name") or "",
        created_by_id=actor.id,
        created_by=actor,
        lines=[],
        report_to=[],
        adjustment=ZERO,
    )
    await _apply(session, invoice, data)
    session.add(invoice)
    await session.flush()
    await log(
        session,
        actor,
        "invoice.created",
        f"Prepared invoice {invoice.number} to {invoice.customer_name}: "
        f"{invoice.currency} {invoice.total}",
        invoice_id=invoice.id,
        run_id=invoice.run_id,
    )
    return invoice


async def update_invoice(
    session: AsyncSession, invoice: PayrollInvoice, data: dict[str, Any], actor: User
) -> PayrollInvoice:
    _require(invoice, InvoiceStatus.DRAFT, doing="changed")
    before = invoice.total
    await _apply(session, invoice, data)
    await session.flush()
    await log(
        session,
        actor,
        "invoice.updated",
        f"Changed invoice {invoice.number}"
        + (
            f": total {invoice.currency} {before} → {invoice.total}"
            if before != invoice.total
            else ""
        ),
        invoice_id=invoice.id,
    )
    return invoice


async def delete_invoice(session: AsyncSession, invoice: PayrollInvoice, actor: User) -> None:
    _require(invoice, InvoiceStatus.DRAFT, doing="deleted")
    await log(
        session,
        actor,
        "invoice.deleted",
        f"Deleted draft invoice {invoice.number} to {invoice.customer_name} "
        f"({invoice.currency} {invoice.total})",
    )
    await session.delete(invoice)
    await session.flush()


# ── approving ─────────────────────────────────────────────────────────


async def submit(
    session: AsyncSession, invoice: PayrollInvoice, actor: User, *, mailer: Any, link: str
) -> PayrollInvoice:
    _require(invoice, InvoiceStatus.DRAFT, doing="sent for approval")
    if not invoice.lines or invoice.total <= 0:
        raise PayrollError("There is nothing to invoice: add a line with an amount.")
    invoice.status = InvoiceStatus.SUBMITTED
    invoice.submitted_by_id = actor.id
    invoice.submitted_by = actor
    invoice.submitted_at = datetime.now(UTC)
    invoice.approved_by_id = None
    invoice.approved_by = None
    invoice.approved_at = None
    invoice.returned_note = None
    await session.flush()

    # The super admins check it. Not the sender: telling yourself is noise.
    approvers = [
        u
        for u in await payroll_access.users_with_roles(session, {"super_admin"})
        if u.id != actor.id
    ]
    mailed = await _mail(
        session,
        lambda to: mailer.invoice_for_approval(
            invoice, sender=actor.entra_object_id, recipients=to, link=link
        ),
        [u.email for u in approvers],
    )
    await log(
        session,
        actor,
        "invoice.submitted",
        f"Sent invoice {invoice.number} for approval: {invoice.currency} {invoice.total}. {mailed}",
        invoice_id=invoice.id,
        detail={"mail": mailed, "to": [u.email for u in approvers]},
    )
    return invoice


async def approve(
    session: AsyncSession, invoice: PayrollInvoice, actor: User, *, own_allowed: bool
) -> PayrollInvoice:
    """``own_allowed``: a super admin may approve what they sent; nobody else may."""
    _require(invoice, InvoiceStatus.SUBMITTED, doing="approved")
    if invoice.submitted_by_id == actor.id and not own_allowed:
        raise PayrollConflict("You sent this invoice, so a super admin has to approve it.")
    invoice.status = InvoiceStatus.APPROVED
    invoice.approved_by_id = actor.id
    invoice.approved_by = actor
    invoice.approved_at = datetime.now(UTC)
    await session.flush()
    await log(
        session,
        actor,
        "invoice.approved",
        f"Approved invoice {invoice.number}: {invoice.currency} {invoice.total}",
        invoice_id=invoice.id,
    )
    return invoice


async def return_invoice(
    session: AsyncSession, invoice: PayrollInvoice, note: str, actor: User
) -> PayrollInvoice:
    _require(invoice, InvoiceStatus.SUBMITTED, doing="sent back")
    invoice.status = InvoiceStatus.DRAFT
    invoice.returned_note = note.strip()
    await session.flush()
    await log(
        session,
        actor,
        "invoice.returned",
        f"Sent invoice {invoice.number} back: {note.strip()}",
        invoice_id=invoice.id,
        detail={"note": note.strip()},
    )
    return invoice


# ── Zoho, and the report ─────────────────────────────────────────────


async def check_against_zoho(zoho: Any, invoice: PayrollInvoice) -> list[str]:
    """What Zoho would refuse, found with reads only.

    The customer must exist, be a customer, be active and be billed in this
    invoice's currency (Zoho invoices a contact in its own currency); every tax
    must be one of the organisation's, at the same rate; every item must exist.
    """
    from app.zoho.client import ZohoError

    problems: list[str] = []
    if not invoice.customer_zoho_id:
        problems.append(
            f"{invoice.customer_name} is not linked to a Zoho contact — choose the customer from Zoho."
        )
    else:
        try:
            contact = await zoho.contact(invoice.customer_zoho_id)
        except ZohoError as exc:
            contact = {}
            problems.append(f"Zoho could not find the customer {invoice.customer_name}: {exc}")
        if contact:
            if contact.get("contact_type") and contact["contact_type"] != "customer":
                problems.append(
                    f"{contact.get('contact_name')} is a {contact['contact_type']} in Zoho, not a customer."
                )
            if contact.get("status") and contact["status"] != "active":
                problems.append(f"{contact.get('contact_name')} is {contact['status']} in Zoho.")
            if contact.get("currency_code") and contact["currency_code"] != invoice.currency:
                problems.append(
                    f"Zoho bills {contact.get('contact_name')} in {contact['currency_code']}, "
                    f"but this invoice is in {invoice.currency}."
                )
    if any(line.get("tax_id") for line in invoice.lines):
        try:
            taxes = {t.get("tax_id"): t for t in await zoho.taxes()}
        except ZohoError as exc:
            taxes = None
            problems.append(f"Zoho's taxes could not be read: {exc}")
        if taxes is not None:
            for line in invoice.lines:
                tax_id = line.get("tax_id")
                if not tax_id:
                    continue
                tax = taxes.get(tax_id)
                if tax is None:
                    problems.append(f"{line['name']}: the tax is not one of Zoho's.")
                elif dec(tax.get("tax_percentage") or 0) != dec(line.get("tax_percentage") or 0):
                    problems.append(
                        f"{line['name']}: Zoho's {tax.get('tax_name')} is {tax.get('tax_percentage')} %, "
                        f"not {line.get('tax_percentage')} %."
                    )
    for line in invoice.lines:
        if line.get("item_id"):
            try:
                await zoho.item(line["item_id"])
            except ZohoError as exc:
                problems.append(f"{line['name']}: Zoho could not find the item ({exc}).")
    return problems


async def to_zoho(
    session: AsyncSession,
    invoice: PayrollInvoice,
    actor: User,
    *,
    zoho: Any,
    live: bool,
    mailer: Any,
    link: str,
) -> PayrollInvoice:
    """Create the approved invoice in Zoho — or, in test mode, check it and stop.

    Then send the report. A test run can be repeated (after fixing what the
    check found); a live one only once, because a second press would make a
    second invoice in the real books.
    """
    from app.zoho.client import ZohoError

    _require(invoice, InvoiceStatus.APPROVED, doing="sent to Zoho")
    if invoice.zoho_mode == "live":
        raise PayrollConflict(
            f"Invoice {invoice.number} is already in Zoho as {invoice.zoho_invoice_number}."
        )
    payload = zoho_payload(invoice)
    invoice.zoho_payload = payload
    problems = await check_against_zoho(zoho, invoice)
    if problems:
        invoice.zoho_error = " ".join(problems)
        await session.flush()
        await log(
            session,
            actor,
            "invoice.zoho_refused",
            f"Invoice {invoice.number} is not ready for Zoho: {invoice.zoho_error}",
            invoice_id=invoice.id,
            detail={"problems": problems, "live": live},
        )
        raise PayrollError("Not ready for Zoho: " + " ".join(problems))

    if live:
        try:
            created = await zoho.create_invoice(payload)
        except ZohoError as exc:
            invoice.zoho_error = str(exc)
            await session.flush()
            raise PayrollError(f"Zoho did not create the invoice: {exc}") from exc
        invoice.zoho_invoice_id = str(created.get("invoice_id") or "") or None
        invoice.zoho_invoice_number = created.get("invoice_number")
        try:
            invoice.zoho_pdf = await zoho.invoice_pdf(invoice.zoho_invoice_id)
        except ZohoError:
            # The invoice exists; only its PDF is missing. The report falls back
            # to ours, and "Send report" can be pressed again later.
            invoice.zoho_pdf = None
    invoice.zoho_mode = "live" if live else "test"
    invoice.zoho_at = datetime.now(UTC)
    invoice.zoho_by_id = actor.id
    invoice.zoho_by = actor
    invoice.zoho_error = None
    await session.flush()
    await log(
        session,
        actor,
        "invoice.zoho",
        (
            f"Created invoice {invoice.number} in Zoho as {invoice.zoho_invoice_number}"
            if live
            else f"Checked invoice {invoice.number} against Zoho (test mode — nothing was written)"
        ),
        invoice_id=invoice.id,
        detail={"live": live, "payload": payload},
    )
    await send_report(session, invoice, actor, mailer=mailer, link=link)
    return invoice


async def report_recipients(session: AsyncSession) -> list[User]:
    """The super admins, the CEO and the Accounts team — once each."""
    people = await payroll_access.users_with_roles(session, {"super_admin", "ceo"})
    people += await payroll_access.accounts_users(session)
    seen: dict[str, User] = {}
    for user in people:
        if user.email and user.email.lower() not in seen:
            seen[user.email.lower()] = user
    return list(seen.values())


async def send_report(
    session: AsyncSession, invoice: PayrollInvoice, actor: User, *, mailer: Any, link: str
) -> PayrollInvoice:
    """The invoice PDF to the super admins, the CEO and Accounts."""
    from app.payroll.invoice_pdf import invoice_pdf

    if invoice.status != InvoiceStatus.APPROVED or not invoice.zoho_mode:
        raise PayrollConflict("The report goes out once the invoice has been through Zoho.")
    settings = await get_settings(session)
    people = await report_recipients(session)
    to = [u.email for u in people]
    pdf = invoice.zoho_pdf or invoice_pdf(invoice, settings, test=invoice.zoho_mode != "live")
    mailed = await _mail(
        session,
        lambda recipients: mailer.invoice_report(
            invoice, sender=actor.entra_object_id, recipients=recipients, link=link, pdf=pdf
        ),
        to,
    )
    invoice.report_to = to
    if mailed.startswith("Mailed"):
        invoice.report_sent_at = datetime.now(UTC)
        invoice.report_error = None
    elif mailed.startswith("Mail failed"):
        invoice.report_error = mailed
    await session.flush()
    await log(
        session,
        actor,
        "invoice.report",
        f"Report for invoice {invoice.number}: {mailed}",
        invoice_id=invoice.id,
        detail={"to": to, "mail": mailed},
    )
    return invoice


async def _mail(session: AsyncSession, send: Any, to: list[str]) -> str:
    """Send, or say why not. Never raises: the step is done either way."""
    from app.core.mail import MailError

    settings: PayrollSettings = await get_settings(session)
    if not to:
        return "Nobody to mail."
    if not settings.invoice_mail:
        return "Mail is off in payroll settings — would have mailed " + ", ".join(to) + "."
    try:
        await send(to)
    except MailError as exc:
        return f"Mail failed: {exc}"
    except Exception as exc:  # noqa: BLE001 — a mail outage must not undo the step
        return f"Mail failed: {exc}"
    return "Mailed " + ", ".join(to) + "."


def summary_html(invoice: PayrollInvoice) -> str:
    """The invoice as a small HTML table, for the mails."""
    cell = "padding:4px 8px;border-bottom:1px solid #e2e9ef"
    rows = []
    for line in invoice.lines:
        name = html.escape(str(line["name"]))
        if line.get("description"):
            name += (
                "<br><span style='color:#64727f'>"
                + html.escape(str(line["description"]))
                + "</span>"
            )
        rows.append(
            f"<tr><td style='{cell}'>{name}</td>"
            f"<td style='{cell};text-align:right'>{dec(line['quantity']).normalize():f}</td>"
            f"<td style='{cell};text-align:right'>{dec(line['rate']):,.2f}</td>"
            f"<td style='{cell};text-align:right'>{dec(line['amount']):,.2f}</td></tr>"
        )
    facts = [
        ("Invoice", f"<b>{html.escape(invoice.number)}</b>"),
        ("Customer", html.escape(invoice.customer_name)),
        ("Date", f"{invoice.invoice_date:%d %b %Y}"),
    ]
    if invoice.due_date:
        facts.append(("Due", f"{invoice.due_date:%d %b %Y}"))
    if dec(invoice.tax_total) > 0:
        facts.append(("Tax", f"{invoice.currency} {dec(invoice.tax_total):,.2f}"))
    facts.append(("Total", f"<b>{invoice.currency} {dec(invoice.total):,.2f}</b>"))
    font = "font-family:Segoe UI,Arial,sans-serif;font-size:13px"
    head = "".join(
        f"<tr><td style='padding:2px 8px;color:#64727f'>{k}</td><td style='padding:2px 8px'>{v}</td></tr>"
        for k, v in facts
    )
    return (
        f"<table cellspacing='0' style='border-collapse:collapse;{font}'>{head}</table><br>"
        f"<table cellspacing='0' style='border-collapse:collapse;{font};min-width:480px'>"
        "<tr style='background:#f5f9fb'><th style='padding:4px 8px;text-align:left'>Item</th>"
        "<th style='padding:4px 8px;text-align:right'>Qty</th>"
        "<th style='padding:4px 8px;text-align:right'>Rate</th>"
        "<th style='padding:4px 8px;text-align:right'>Amount</th></tr>" + "".join(rows) + "</table>"
    )
