"""Payroll invoices' API, and the Zoho look-ups the invoice form needs.

The same access as the rest of payroll (``app/payroll/access.py``): readers see
invoices, processors prepare them, super admins or the CEO approve, and either
side presses "Create in Zoho" once one is approved. See
``app/payroll/invoicing.py`` for the steps and what test mode means.
"""

from __future__ import annotations

import time
import uuid
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import CurrentUser
from app.core.config import get_settings as app_settings
from app.core.db import get_session
from app.models.payroll import PayrollInvoice
from app.payroll import invoicing, service
from app.payroll.access import Approver, PreparerOrApprover, Processor, Viewer
from app.payroll.invoice_pdf import invoice_pdf
from app.payroll.router import _event_out, _translate
from app.payroll.schemas import (
    InvoiceDetailOut,
    InvoiceIn,
    InvoiceLineOut,
    InvoiceOut,
    InvoiceSetupOut,
    ReturnIn,
    ZohoContactOut,
    ZohoItemOut,
    ZohoTaxOut,
)
from app.payroll.service import PayrollError
from app.zoho.client import ZohoBooks, ZohoError

router = APIRouter(prefix="/payroll", tags=["payroll"])

Session = Annotated[AsyncSession, Depends(get_session)]


def get_zoho(request: Request) -> ZohoBooks:
    return request.app.state.zoho


def get_mailer(request: Request) -> Any:
    return request.app.state.payroll_mailer


Zoho = Annotated[ZohoBooks, Depends(get_zoho)]
Mailer = Annotated[Any, Depends(get_mailer)]


def _link(invoice: PayrollInvoice) -> str:
    return f"{app_settings().frontend_url.rstrip('/')}/payroll?tab=invoices&invoice={invoice.id}"


def _name(user: Any) -> str | None:
    return user.display_name if user else None


def _out(invoice: PayrollInvoice) -> InvoiceOut:
    return InvoiceOut(
        id=invoice.id,
        number=invoice.number,
        status=invoice.status,
        customer_zoho_id=invoice.customer_zoho_id,
        customer_name=invoice.customer_name,
        currency=invoice.currency,
        exchange_rate=invoice.exchange_rate,
        invoice_date=invoice.invoice_date,
        due_date=invoice.due_date,
        payment_terms=invoice.payment_terms,
        reference_number=invoice.reference_number,
        adjustment=invoice.adjustment,
        adjustment_description=invoice.adjustment_description,
        notes=invoice.notes,
        terms=invoice.terms,
        sub_total=invoice.sub_total,
        tax_total=invoice.tax_total,
        total=invoice.total,
        run_id=invoice.run_id,
        created_by_name=_name(invoice.created_by),
        submitted_by_id=invoice.submitted_by_id,
        submitted_by_name=_name(invoice.submitted_by),
        submitted_at=invoice.submitted_at,
        approved_by_name=_name(invoice.approved_by),
        approved_at=invoice.approved_at,
        returned_note=invoice.returned_note,
        zoho_mode=invoice.zoho_mode,
        zoho_invoice_id=invoice.zoho_invoice_id,
        zoho_invoice_number=invoice.zoho_invoice_number,
        zoho_at=invoice.zoho_at,
        zoho_by_name=_name(invoice.zoho_by),
        zoho_error=invoice.zoho_error,
        report_sent_at=invoice.report_sent_at,
        report_to=list(invoice.report_to or []),
        report_error=invoice.report_error,
        created_at=invoice.created_at,
    )


async def _detail(session: AsyncSession, invoice: PayrollInvoice) -> InvoiceDetailOut:
    events = await service.events(session, invoice_id=invoice.id)
    return InvoiceDetailOut(
        **_out(invoice).model_dump(),
        lines=[InvoiceLineOut.model_validate(line) for line in invoice.lines],
        zoho_payload=invoice.zoho_payload,
        events=[_event_out(e) for e in events],
    )


async def _invoice(session: AsyncSession, invoice_id: uuid.UUID) -> PayrollInvoice:
    try:
        return await invoicing.get_invoice(session, invoice_id)
    except PayrollError as exc:
        raise _translate(exc) from exc


# ── invoices ──────────────────────────────────────────────────────────


@router.get("/invoices", response_model=list[InvoiceOut])
async def list_invoices(_: Viewer, session: Session) -> list[InvoiceOut]:
    return [_out(i) for i in await invoicing.list_invoices(session)]


@router.post("/invoices", response_model=InvoiceDetailOut, status_code=status.HTTP_201_CREATED)
async def create_invoice(
    body: InvoiceIn, _: Processor, user: CurrentUser, session: Session
) -> InvoiceDetailOut:
    try:
        invoice = await invoicing.create_invoice(session, body.model_dump(mode="python"), user)
    except PayrollError as exc:
        raise _translate(exc) from exc
    return await _detail(session, invoice)


@router.get("/invoices/{invoice_id}", response_model=InvoiceDetailOut)
async def get_invoice(invoice_id: uuid.UUID, _: Viewer, session: Session) -> InvoiceDetailOut:
    return await _detail(session, await _invoice(session, invoice_id))


@router.patch("/invoices/{invoice_id}", response_model=InvoiceDetailOut)
async def update_invoice(
    invoice_id: uuid.UUID, body: InvoiceIn, _: Processor, user: CurrentUser, session: Session
) -> InvoiceDetailOut:
    invoice = await _invoice(session, invoice_id)
    try:
        await invoicing.update_invoice(session, invoice, body.model_dump(mode="python"), user)
    except PayrollError as exc:
        raise _translate(exc) from exc
    return await _detail(session, invoice)


@router.delete("/invoices/{invoice_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_invoice(
    invoice_id: uuid.UUID, _: Processor, user: CurrentUser, session: Session
) -> None:
    invoice = await _invoice(session, invoice_id)
    try:
        await invoicing.delete_invoice(session, invoice, user)
    except PayrollError as exc:
        raise _translate(exc) from exc


@router.post("/invoices/{invoice_id}/submit", response_model=InvoiceDetailOut)
async def submit_invoice(
    invoice_id: uuid.UUID, _: Processor, user: CurrentUser, session: Session, mailer: Mailer
) -> InvoiceDetailOut:
    invoice = await _invoice(session, invoice_id)
    try:
        await invoicing.submit(session, invoice, user, mailer=mailer, link=_link(invoice))
    except PayrollError as exc:
        raise _translate(exc) from exc
    return await _detail(session, invoice)


@router.post("/invoices/{invoice_id}/approve", response_model=InvoiceDetailOut)
async def approve_invoice(
    invoice_id: uuid.UUID, access: Approver, user: CurrentUser, session: Session
) -> InvoiceDetailOut:
    invoice = await _invoice(session, invoice_id)
    try:
        await invoicing.approve(session, invoice, user, own_allowed=access.can_admin)
    except PayrollError as exc:
        raise _translate(exc) from exc
    return await _detail(session, invoice)


@router.post("/invoices/{invoice_id}/return", response_model=InvoiceDetailOut)
async def return_invoice(
    invoice_id: uuid.UUID, body: ReturnIn, _: Approver, user: CurrentUser, session: Session
) -> InvoiceDetailOut:
    invoice = await _invoice(session, invoice_id)
    try:
        await invoicing.return_invoice(session, invoice, body.note, user)
    except PayrollError as exc:
        raise _translate(exc) from exc
    return await _detail(session, invoice)


@router.post("/invoices/{invoice_id}/zoho", response_model=InvoiceDetailOut)
async def invoice_to_zoho(
    invoice_id: uuid.UUID,
    _: PreparerOrApprover,
    user: CurrentUser,
    session: Session,
    zoho: Zoho,
    mailer: Mailer,
) -> InvoiceDetailOut:
    invoice = await _invoice(session, invoice_id)
    try:
        await invoicing.to_zoho(
            session,
            invoice,
            user,
            zoho=zoho,
            live=app_settings().payroll_zoho_invoice_write,
            mailer=mailer,
            link=_link(invoice),
        )
    except PayrollError as exc:
        # Keep what the check found, and the log line saying so: the request
        # fails, but "why" should still be on the invoice afterwards.
        await session.commit()
        raise _translate(exc) from exc
    return await _detail(session, invoice)


@router.post("/invoices/{invoice_id}/report", response_model=InvoiceDetailOut)
async def invoice_report(
    invoice_id: uuid.UUID,
    _: PreparerOrApprover,
    user: CurrentUser,
    session: Session,
    mailer: Mailer,
) -> InvoiceDetailOut:
    invoice = await _invoice(session, invoice_id)
    try:
        await invoicing.send_report(session, invoice, user, mailer=mailer, link=_link(invoice))
    except PayrollError as exc:
        raise _translate(exc) from exc
    return await _detail(session, invoice)


@router.get("/invoices/{invoice_id}/pdf")
async def invoice_file(invoice_id: uuid.UUID, _: Viewer, session: Session) -> Response:
    invoice = await _invoice(session, invoice_id)
    if invoice.zoho_pdf:
        content, name = invoice.zoho_pdf, f"{invoice.zoho_invoice_number or invoice.number}.pdf"
    else:
        settings = await service.get_settings(session)
        test = invoice.zoho_mode != "live"
        content = invoice_pdf(invoice, settings, test=test)
        name = f"{invoice.number}{' TEST' if test else ''}.pdf"
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"},
    )


# ── what the form needs ───────────────────────────────────────────────

#: Zoho's taxes, currencies and organisation name change rarely; five minutes
#: spares every form opening three calls against a 100-a-minute rate limit.
_CACHE: dict[str, tuple[float, Any]] = {}
_TTL = 300.0


async def _cached(key: str, load: Any) -> Any:
    hit = _CACHE.get(key)
    if hit and time.monotonic() - hit[0] < _TTL:
        return hit[1]
    value = await load()
    _CACHE[key] = (time.monotonic(), value)
    return value


@router.get("/invoice-setup", response_model=InvoiceSetupOut)
async def invoice_setup(_: Viewer, session: Session, zoho: Zoho) -> InvoiceSetupOut:
    settings = await service.get_settings(session)
    taxes: list[ZohoTaxOut] = []
    currencies: list[str] = []
    org: str | None = None
    error: str | None = None
    if not zoho.configured:
        error = "Zoho Books is not configured on this server."
    else:
        try:
            raw_taxes = await _cached("taxes", zoho.taxes)
            taxes = [
                ZohoTaxOut(
                    tax_id=str(t["tax_id"]),
                    tax_name=t.get("tax_name") or "Tax",
                    tax_percentage=t.get("tax_percentage") or 0,
                )
                for t in raw_taxes
                if t.get("tax_id")
            ]
            raw_currencies = await _cached("currencies", zoho.currencies)
            currencies = [c["currency_code"] for c in raw_currencies if c.get("currency_code")]
            orgs = await _cached("org", lambda: zoho._get("/organizations"))
            for o in orgs.get("organizations") or []:
                if str(o.get("organization_id")) == str(zoho.organization_id):
                    org = f"{o.get('name')} ({o.get('currency_code')})"
        except ZohoError as exc:
            error = str(exc)
    approvers = await invoicing.payroll_access.users_with_roles(session, {"super_admin"})
    report = await invoicing.report_recipients(session)
    return InvoiceSetupOut(
        zoho_live=app_settings().payroll_zoho_invoice_write,
        zoho_organization=org,
        mail=settings.invoice_mail,
        zoho_error=error,
        taxes=taxes,
        currencies=currencies,
        default_customer_zoho_id=settings.invoice_customer_zoho_id,
        default_customer_name=settings.invoice_customer_name,
        payment_terms=settings.invoice_payment_terms,
        notes=settings.invoice_notes,
        terms=settings.invoice_terms,
        approvers=[u.email for u in approvers],
        report_to=[u.email for u in report],
    )


@router.get("/zoho/customers", response_model=list[ZohoContactOut])
async def zoho_customers(
    _: Viewer, zoho: Zoho, q: Annotated[str, Query(max_length=100)] = ""
) -> list[ZohoContactOut]:
    try:
        rows = await zoho.search_contacts(q.strip())
    except ZohoError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return [
        ZohoContactOut(
            contact_id=str(r["contact_id"]),
            contact_name=r.get("contact_name") or "",
            company_name=r.get("company_name") or None,
            currency_code=r.get("currency_code") or None,
            email=r.get("email") or None,
            tax_treatment=r.get("tax_treatment") or None,
        )
        for r in rows
        if r.get("contact_id")
    ]


@router.get("/zoho/items", response_model=list[ZohoItemOut])
async def zoho_items(
    _: Viewer, zoho: Zoho, q: Annotated[str, Query(max_length=100)] = ""
) -> list[ZohoItemOut]:
    try:
        rows = await zoho.search_items(q.strip())
    except ZohoError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return [
        ZohoItemOut(
            item_id=str(r["item_id"]),
            name=r.get("name") or "",
            description=r.get("description") or None,
            rate=r.get("rate"),
            tax_id=str(r["tax_id"]) if r.get("tax_id") else None,
        )
        for r in rows
        if r.get("item_id")
    ]
