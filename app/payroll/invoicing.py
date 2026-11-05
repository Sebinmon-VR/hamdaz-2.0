"""The invoice a payroll month is billed on, and its way into Zoho Books.

The India payroll is not only paid, it is invoiced: Hamdaz Technologies LLP
(the India company in payroll settings) bills Hamdaztech Technologies LLC (the
UAE company) for the month's salaries and the other charges it carried — rent,
internet, anything (the user, 2026-11-05: "we need the payroll and the invoice
together"). So each run has one invoice, prepared with it and **approved with
it**:

1. The accountant prepares the month — the pay sheet, and under it the
   invoice: the salary lines come off the payslips, the other charges are
   typed in.
2. Sending the month for approval **mails the super admins**.
3. A super admin (or the CEO) approves the month — payroll and invoice.
4. **"Create in Zoho"** creates the invoice in Zoho Books and fetches Zoho's PDF.
5. **The report** — that PDF and the salary register, mailed to the super
   admins, the CEO and the Accounts team.

The salary lines are never stored: ``salary_lines`` reads them off the run
each time, so the invoice cannot disagree with the payroll it bills.

Step 4 is **test mode** unless ``PAYROLL_ZOHO_INVOICE_WRITE`` is on, and it
ships off — Zoho is the live production books. In test mode the invoice is
still checked against Zoho with reads (customer, currency, every tax, every
item) and the exact body Zoho would receive is kept; nothing is written, and
the report carries this app's own PDF, marked TEST. Mail in steps 2 and 5 sits
behind ``PayrollSettings.mail_enabled``, also off; with it off each step
records who it would have mailed.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.payroll import PayrollInvoice, PayrollRun, PayrollSettings, RunStatus
from app.models.user import User
from app.payroll import access as payroll_access
from app.payroll.rules import dec, r2, to_json
from app.payroll.service import (
    PayrollConflict,
    PayrollError,
    _country,
    get_settings,
    log,
    period_of,
)

ZERO = Decimal("0")

# ── the arithmetic ────────────────────────────────────────────────────


def clean_charges(raw: Iterable[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Each charge checked and its amount and tax worked out, the way Zoho does.

    ``amount`` is quantity × rate to the cent; ``tax_amount`` that amount ×
    the tax rate, to the cent, per line — Zoho rounds per line too, so the
    totals agree with the invoice Zoho shows.
    """
    out: list[dict[str, Any]] = []
    for index, item in enumerate(raw or [], start=1):
        name = str(item.get("name") or "").strip()
        if not name:
            raise PayrollError(f"Charge {index} needs a name.")
        try:
            quantity = dec(item.get("quantity") if item.get("quantity") not in (None, "") else 1)
            rate = dec(item.get("rate") if item.get("rate") not in (None, "") else 0)
            pct = dec(item.get("tax_percentage") or 0)
        except Exception as exc:  # noqa: BLE001
            raise PayrollError(f"Charge {index} ({name}): a number is not a number.") from exc
        if quantity <= 0:
            raise PayrollError(f"Charge {index} ({name}): the quantity must be more than 0.")
        if rate < 0:
            raise PayrollError(f"Charge {index} ({name}): the rate cannot be negative.")
        if pct < 0 or pct > 100:
            raise PayrollError(f"Charge {index} ({name}): the tax rate must be 0 to 100 %.")
        tax_id = str(item.get("tax_id") or "").strip() or None
        out.append(_line("charge", item, name, quantity, rate, tax_id, pct if tax_id else ZERO))
    return to_json(out)


def _line(
    source: str,
    item: dict[str, Any],
    name: str,
    quantity: Decimal,
    rate: Decimal,
    tax_id: str | None,
    pct: Decimal,
) -> dict[str, Any]:
    amount = r2(quantity * rate)
    return {
        "source": source,
        "item_id": str(item.get("item_id") or "").strip() or None,
        "name": name[:200],
        "description": str(item.get("description") or "").strip() or None,
        "quantity": quantity.normalize() if quantity == quantity.to_integral() else quantity,
        "rate": r2(rate),
        "tax_id": tax_id,
        "tax_name": (str(item.get("tax_name") or "").strip() or None) if tax_id else None,
        "tax_percentage": pct,
        "amount": amount,
        "tax_amount": r2(amount * pct / 100) if tax_id else ZERO,
    }


def convert(amount: Any, *, run_currency: str, currency: str, rate: Any) -> Decimal | None:
    """A payroll amount in the invoice's currency; ``None`` when there is no rate."""
    if currency == run_currency:
        return r2(amount)
    if not rate or dec(rate) <= 0:
        return None
    return r2(dec(amount) / dec(rate))


def salary_lines(run: PayrollRun, invoice: Any) -> tuple[list[dict[str, Any]], bool]:
    """The salary lines, read off the payslips; and whether a rate is missing.

    What is billed is each person's cost to the company — gross pay plus the
    employer's contributions, which with no statutory deductions is the gross.
    """
    period = period_of(run)
    missing = False

    def money(value: Any) -> Decimal:
        nonlocal missing
        got = convert(value, run_currency=run.currency, currency=invoice.currency, rate=invoice.exchange_rate)
        if got is None:
            missing = True
            return ZERO
        return got

    if not run.lines:
        return [], False
    if invoice.salary_lines == "per_employee":
        out = []
        for line in run.lines:
            what = " · ".join(x for x in (line.employee_code, line.designation) if x)
            out.append(
                _line(
                    "salary",
                    {"description": f"{what} — salary, {period.label}"},
                    line.employee_name,
                    Decimal("1"),
                    money(line.employer_cost),
                    None,
                    ZERO,
                )
            )
        return to_json(out), missing
    people = len(run.lines)
    line = _line(
        "salary",
        {"description": f"{_country(run.country)} payroll, {period.label} — "
                        f"{people} {'employee' if people == 1 else 'employees'}"},
        "Salaries",
        Decimal("1"),
        money(run.employer_cost),
        None,
        ZERO,
    )
    return to_json([line]), missing


def all_lines(run: PayrollRun, invoice: Any) -> tuple[list[dict[str, Any]], bool]:
    salaries, missing = salary_lines(run, invoice)
    return salaries + list(invoice.charges or []), missing


def totals(lines: list[dict[str, Any]], adjustment: Any = 0) -> tuple[Decimal, Decimal, Decimal]:
    """(sub total, tax, total). The adjustment is added after tax, as in Zoho."""
    sub = r2(sum((dec(line["amount"]) for line in lines), ZERO))
    tax = r2(sum((dec(line["tax_amount"]) for line in lines), ZERO))
    return sub, tax, r2(sub + tax + dec(adjustment or 0))


def zoho_payload(invoice: Any, lines: list[dict[str, Any]], *, run_currency: str) -> dict[str, Any]:
    """The body for Zoho's ``POST /invoices`` — exactly what would be sent.

    Zoho numbers its invoices itself, so ours goes in as the reference when
    there is no other. ``exchange_rate`` is only sent for a foreign-currency
    invoice; it is units of the issuing company's base currency (INR, the
    payroll's) per unit of the invoice's, which is what Zoho means by it.
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
    if invoice.currency != run_currency and invoice.exchange_rate:
        body["exchange_rate"] = num(invoice.exchange_rate)
    for line in lines:
        item: dict[str, Any] = {
            "name": line["name"],
            "quantity": num(line["quantity"]),
            "rate": num(line["rate"]),
        }
        for key in ("item_id", "description", "tax_id"):
            if line.get(key):
                item[key] = line[key]
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


# ── the invoice of a month ────────────────────────────────────────────


async def get_for_run(session: AsyncSession, run: PayrollRun) -> PayrollInvoice | None:
    return await session.scalar(select(PayrollInvoice).where(PayrollInvoice.run_id == run.id))


def defaults(run: PayrollRun, settings: PayrollSettings) -> dict[str, Any]:
    """A new month's invoice: from the India company to the UAE company."""
    period = period_of(run)
    return {
        "customer_zoho_id": settings.invoice_customer_zoho_id,
        "customer_name": settings.ae_company_name,
        "customer_address": settings.ae_company_address,
        "currency": settings.invoice_currency or run.currency,
        "exchange_rate": None,
        "invoice_date": period.end,
        "due_date": None,
        "payment_terms": settings.invoice_payment_terms,
        "reference_number": f"Payroll {period.label}",
        "salary_lines": "single",
        "charges": [],
        "adjustment": ZERO,
        "adjustment_description": None,
        "notes": settings.invoice_notes,
        "terms": settings.invoice_terms,
    }


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


def _refresh(invoice: PayrollInvoice, run: PayrollRun) -> list[dict[str, Any]]:
    lines, _ = all_lines(run, invoice)
    invoice.sub_total, invoice.tax_total, invoice.total = totals(lines, invoice.adjustment)
    return lines


async def ensure(session: AsyncSession, run: PayrollRun, actor: User | None) -> PayrollInvoice:
    """The month's invoice, made from the defaults the first time it is needed."""
    invoice = await get_for_run(session, run)
    if invoice is None:
        settings = await get_settings(session)
        values = defaults(run, settings)
        invoice = PayrollInvoice(
            run_id=run.id,
            number=await next_number(session, settings.invoice_prefix, values["invoice_date"].year),
            report_to=[],
            updated_by_id=actor.id if actor else None,
            **values,
        )
        _refresh(invoice, run)
        session.add(invoice)
        await session.flush()
    return invoice


_FIELDS = (
    "customer_zoho_id", "customer_name", "customer_address", "currency", "exchange_rate",
    "invoice_date", "due_date", "payment_terms", "reference_number", "salary_lines",
    "adjustment", "adjustment_description", "notes", "terms",
)


async def save(
    session: AsyncSession, run: PayrollRun, data: dict[str, Any], actor: User
) -> PayrollInvoice:
    """The accountant's changes. Only while the month is a draft — it is approved with it."""
    if run.status != RunStatus.DRAFT:
        raise PayrollConflict(
            f"The {period_of(run).label} payroll is {run.status}; its invoice is locked with it."
        )
    invoice = await ensure(session, run, actor)
    for key in _FIELDS:
        if key in data:
            value = data[key]
            if isinstance(value, str):
                value = value.strip() or None
            setattr(invoice, key, value)
    if not invoice.customer_name:
        raise PayrollError("The invoice needs a customer.")
    invoice.currency = (invoice.currency or "").upper()
    if not re.fullmatch(r"[A-Z]{3}", invoice.currency):
        raise PayrollError("The currency is a three-letter code, like INR or AED.")
    if invoice.due_date and invoice.due_date < invoice.invoice_date:
        raise PayrollError("The due date is before the invoice date.")
    if invoice.salary_lines not in ("single", "per_employee"):
        raise PayrollError("Salaries are billed as one line or a line per employee.")
    if "charges" in data:
        invoice.charges = clean_charges(data["charges"])
    invoice.adjustment = r2(invoice.adjustment or 0)
    invoice.updated_by_id = actor.id
    _refresh(invoice, run)
    await session.flush()
    await log(
        session, actor, "invoice.updated",
        f"Changed the invoice for {period_of(run).label} ({invoice.number}): "
        f"{invoice.currency} {invoice.total}",
        run_id=run.id,
    )
    return invoice


# ── approval ──────────────────────────────────────────────────────────


async def on_submit(
    session: AsyncSession, run: PayrollRun, actor: User, *, mailer: Any, link: str, notify: bool
) -> None:
    """After a month is sent for approval: its invoice is settled, the super admins told.

    ``notify`` is off when a super admin approves their own month in one step:
    asking the other super admins to approve something already approved is noise.
    """
    invoice = await ensure(session, run, actor)
    lines = _refresh(invoice, run)
    await session.flush()
    if not notify:
        return
    approvers = [
        u for u in await payroll_access.users_with_roles(session, {"super_admin"}) if u.id != actor.id
    ]
    to = [u.email for u in approvers]
    mailed = await _mail(
        session,
        lambda recipients: mailer.month_for_approval(
            run, invoice, lines, sender=actor.entra_object_id, recipients=recipients, link=link
        ),
        to,
    )
    await log(
        session, actor, "run.approval_mail",
        f"Approval request for {period_of(run).label}: {mailed}",
        run_id=run.id, detail={"to": to, "mail": mailed},
    )


async def on_reopen(session: AsyncSession, run: PayrollRun) -> None:
    """A reopened month's test result is stale; a live Zoho invoice stays on record."""
    invoice = await get_for_run(session, run)
    if invoice is not None and invoice.zoho_mode == "test":
        invoice.zoho_mode = None
        invoice.zoho_payload = None
        invoice.zoho_at = None
        invoice.zoho_error = None
        invoice.report_sent_at = None
        invoice.report_to = []
        invoice.report_error = None
        await session.flush()


# ── Zoho, and the report ─────────────────────────────────────────────


async def check_against_zoho(zoho: Any, invoice: Any, lines: list[dict[str, Any]]) -> list[str]:
    """What Zoho would refuse, found with reads only.

    The customer must exist, be a customer, be active and be billed in this
    invoice's currency (Zoho invoices a contact in its own currency); every tax
    must be one of the organisation's, at the same rate; every item must exist.
    """
    from app.zoho.client import ZohoError

    problems: list[str] = []
    if not invoice.customer_zoho_id:
        problems.append(
            f"{invoice.customer_name} is not linked to a Zoho contact — "
            "link the customer in the invoice or in payroll settings."
        )
    else:
        try:
            contact = await zoho.contact(invoice.customer_zoho_id)
        except ZohoError as exc:
            contact = {}
            problems.append(f"Zoho could not find the customer {invoice.customer_name}: {exc}")
        if contact:
            name = contact.get("contact_name")
            if contact.get("contact_type") and contact["contact_type"] != "customer":
                problems.append(f"{name} is a {contact['contact_type']} in Zoho, not a customer.")
            if contact.get("status") and contact["status"] != "active":
                problems.append(f"{name} is {contact['status']} in Zoho.")
            if contact.get("currency_code") and contact["currency_code"] != invoice.currency:
                problems.append(
                    f"Zoho bills {name} in {contact['currency_code']}, "
                    f"but this invoice is in {invoice.currency}."
                )
    if any(line.get("tax_id") for line in lines):
        try:
            taxes = {t.get("tax_id"): t for t in await zoho.taxes()}
        except ZohoError as exc:
            taxes = None
            problems.append(f"Zoho's taxes could not be read: {exc}")
        if taxes is not None:
            for line in lines:
                tax_id = line.get("tax_id")
                if not tax_id:
                    continue
                tax = taxes.get(tax_id)
                if tax is None:
                    problems.append(f"{line['name']}: the tax is not one of Zoho's.")
                elif dec(tax.get("tax_percentage") or 0) != dec(line.get("tax_percentage") or 0):
                    problems.append(
                        f"{line['name']}: Zoho's {tax.get('tax_name')} is "
                        f"{tax.get('tax_percentage')} %, not {line.get('tax_percentage')} %."
                    )
    for line in lines:
        if line.get("item_id"):
            try:
                await zoho.item(line["item_id"])
            except ZohoError as exc:
                problems.append(f"{line['name']}: Zoho could not find the item ({exc}).")
    return problems


async def to_zoho(
    session: AsyncSession,
    run: PayrollRun,
    actor: User,
    *,
    zoho: Any,
    live: bool,
    mailer: Any,
    link: str,
) -> PayrollInvoice:
    """Create the approved month's invoice in Zoho — or, in test mode, check it and stop.

    Then send the report. A test run can be repeated (after fixing what the
    check found); a live one only once, because a second press would make a
    second invoice in the real books.
    """
    from app.zoho.client import ZohoError

    if run.status not in (RunStatus.APPROVED, RunStatus.PAID):
        raise PayrollConflict("The invoice goes to Zoho once the month is approved.")
    invoice = await ensure(session, run, actor)
    if invoice.zoho_mode == "live":
        raise PayrollConflict(
            f"Invoice {invoice.number} is already in Zoho as {invoice.zoho_invoice_number}."
        )
    lines, missing = all_lines(run, invoice)
    invoice.sub_total, invoice.tax_total, invoice.total = totals(lines, invoice.adjustment)
    payload = zoho_payload(invoice, lines, run_currency=run.currency)
    invoice.zoho_payload = payload
    problems = await check_against_zoho(zoho, invoice, lines)
    if missing:
        problems.insert(
            0,
            f"The invoice is in {invoice.currency} but the payroll is in {run.currency}: "
            "give the exchange rate.",
        )
    if problems:
        invoice.zoho_error = " ".join(problems)
        await session.flush()
        await log(
            session, actor, "invoice.zoho_refused",
            f"Invoice {invoice.number} is not ready for Zoho: {invoice.zoho_error}",
            run_id=run.id, detail={"problems": problems, "live": live},
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
        session, actor, "invoice.zoho",
        (
            f"Created invoice {invoice.number} in Zoho as {invoice.zoho_invoice_number}"
            if live
            else f"Checked invoice {invoice.number} against Zoho (test mode — nothing was written)"
        ),
        run_id=run.id, detail={"live": live, "payload": payload},
    )
    await send_report(session, run, invoice, actor, mailer=mailer, link=link)
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
    session: AsyncSession, run: PayrollRun, invoice: PayrollInvoice, actor: User, *, mailer: Any, link: str
) -> PayrollInvoice:
    """The invoice PDF and the salary register to the super admins, the CEO and Accounts."""
    from app.core.mail import Attachment
    from app.payroll.exports import XLSX_TYPE, file_stem, register_xlsx
    from app.payroll.invoice_pdf import invoice_pdf

    if not invoice.zoho_mode:
        raise PayrollConflict("The report goes out once the invoice has been through Zoho.")
    settings = await get_settings(session)
    lines, _ = all_lines(run, invoice)
    to = [u.email for u in await report_recipients(session)]
    test = invoice.zoho_mode != "live"
    pdf = invoice.zoho_pdf or invoice_pdf(run, invoice, lines, settings, test=test)
    files = [
        Attachment(
            name=f"{invoice.zoho_invoice_number or invoice.number}{' TEST' if test else ''}.pdf",
            content=pdf,
            content_type="application/pdf",
        ),
        Attachment(
            name=f"{file_stem(run)} register.xlsx",
            content=register_xlsx(run, settings),
            content_type=XLSX_TYPE,
        ),
    ]
    mailed = await _mail(
        session,
        lambda recipients: mailer.month_report(
            run, invoice, lines, sender=actor.entra_object_id, recipients=recipients,
            link=link, attachments=files,
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
        session, actor, "invoice.report",
        f"Report for {period_of(run).label} ({invoice.number}): {mailed}",
        run_id=run.id, detail={"to": to, "mail": mailed},
    )
    return invoice


async def _mail(session: AsyncSession, send: Any, to: list[str]) -> str:
    """Send, or say why not. Never raises: the step is done either way."""
    from app.core.mail import MailError

    settings = await get_settings(session)
    if not to:
        return "Nobody to mail."
    if not settings.mail_enabled:
        return "Mail is off in payroll settings — would have mailed " + ", ".join(to) + "."
    try:
        await send(to)
    except MailError as exc:
        return f"Mail failed: {exc}"
    except Exception as exc:  # noqa: BLE001 — a mail outage must not undo the step
        return f"Mail failed: {exc}"
    return "Mailed " + ", ".join(to) + "."


def summary_html(run: PayrollRun, invoice: PayrollInvoice, lines: list[dict[str, Any]]) -> str:
    """The month and its invoice as small HTML tables, for the mails."""
    cell = "padding:4px 8px;border-bottom:1px solid #e2e9ef"
    font = "font-family:Segoe UI,Arial,sans-serif;font-size:13px"
    rows = []
    for line in lines:
        name = html.escape(str(line["name"]))
        if line.get("description"):
            name += "<br><span style='color:#64727f'>" + html.escape(str(line["description"])) + "</span>"
        rows.append(
            f"<tr><td style='{cell}'>{name}</td>"
            f"<td style='{cell};text-align:right'>{dec(line['amount']):,.2f}</td></tr>"
        )
    facts = [
        ("Payroll", f"{_country(run.country)} {period_of(run).label} — {run.headcount} people"),
        ("Gross pay", f"{run.currency} {dec(run.gross):,.2f}"),
        ("Net pay", f"{run.currency} {dec(run.net):,.2f}"),
        ("Invoice", f"<b>{html.escape(invoice.number)}</b> to {html.escape(invoice.customer_name)}"),
    ]
    if dec(invoice.tax_total) > 0:
        facts.append(("Tax", f"{invoice.currency} {dec(invoice.tax_total):,.2f}"))
    facts.append(("Invoice total", f"<b>{invoice.currency} {dec(invoice.total):,.2f}</b>"))
    head = "".join(
        f"<tr><td style='padding:2px 8px;color:#64727f'>{k}</td>"
        f"<td style='padding:2px 8px'>{v}</td></tr>"
        for k, v in facts
    )
    return (
        f"<table cellspacing='0' style='border-collapse:collapse;{font}'>{head}</table><br>"
        f"<table cellspacing='0' style='border-collapse:collapse;{font};min-width:420px'>"
        "<tr style='background:#f5f9fb'><th style='padding:4px 8px;text-align:left'>Invoice line</th>"
        "<th style='padding:4px 8px;text-align:right'>Amount</th></tr>"
        + "".join(rows)
        + "</table>"
    )
