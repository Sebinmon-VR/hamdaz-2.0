"""A payroll month's invoice, and the Zoho look-ups its form needs.

The invoice belongs to its run (``/payroll/runs/{id}/invoice``): it is edited
while the month is a draft, approved with the month, and only then created in
Zoho. Access is the rest of payroll's (``app/payroll/access.py``). See
``app/payroll/invoicing.py`` for the steps and what test mode means.
"""

from __future__ import annotations

import time
import uuid
from datetime import date
from types import SimpleNamespace
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import CurrentUser
from app.core.config import get_settings as app_settings
from app.core.db import get_session
from app.models.payroll import PayrollInvoice, PayrollRun
from app.payroll import invoicing, service
from app.payroll.access import PreparerOrApprover, Processor, Viewer, users_with_roles
from app.payroll.deps import (  # noqa: F401 — re-exported for tests
    Mailer,
    Zoho,
    get_mailer,
    get_zoho,
)
from app.payroll.invoice_pdf import invoice_pdf
from app.payroll.router import _run, _translate
from app.payroll.rules import dec
from app.payroll.schemas import (
    ChargeIn,
    InvoiceLineOut,
    InvoiceSetupOut,
    RunInvoiceIn,
    RunInvoiceOut,
    ZohoContactOut,
    ZohoItemOut,
    ZohoTaxOut,
)
from app.payroll.service import PayrollError
from app.zoho.client import ZohoError

router = APIRouter(prefix="/payroll", tags=["payroll"])

Session = Annotated[AsyncSession, Depends(get_session)]



_RATES: dict[tuple[str, str, str], tuple[float, Any]] = {}


async def zoho_rate(zoho: Any, currency: str, run_currency: str, on: date) -> tuple[Any, str | None]:
    """Zoho's rate for the invoice date (INR per invoice-currency unit), cached ten minutes.

    Every invoice view needs it and Zoho allows 100 calls a minute; a rate set
    in Zoho shows here within ten minutes.
    """
    if currency == run_currency:
        return None, None
    if zoho is None or not getattr(zoho, "configured", True):
        return None, "Zoho Books is not connected, so there is no rate to read — type one."
    key = (currency, run_currency, on.isoformat())
    hit = _RATES.get(key)
    if hit and time.monotonic() - hit[0] < 600:
        return hit[1]
    value = await invoicing.zoho_rate_on(zoho, currency=currency, run_currency=run_currency, on=on)
    _RATES[key] = (time.monotonic(), value)
    return value


def month_link(run: PayrollRun) -> str:
    return f"{app_settings().frontend_url.rstrip('/')}/payroll?tab=run&run={run.id}"


def _virtual(run: PayrollRun, settings: Any) -> Any:
    """The invoice a month will get, before anybody has saved it."""
    return SimpleNamespace(
        id=None, number=None, zoho_mode=None, zoho_payload=None, zoho_invoice_id=None,
        zoho_invoice_number=None, zoho_at=None, zoho_by=None, zoho_error=None,
        report_sent_at=None, report_to=[], report_error=None,
        **invoicing.defaults(run, settings),
    )


async def _out(session: AsyncSession, run: PayrollRun, zoho: Any) -> RunInvoiceOut:
    settings = await service.get_settings(session)
    saved = await invoicing.get_for_run(session, run)
    inv: Any = saved or _virtual(run, settings)
    rate, note = await zoho_rate(zoho, inv.currency, run.currency, inv.invoice_date)
    if saved is None and rate:
        inv.exchange_rate = rate  # what the invoice will be made with
    lines, missing = invoicing.all_lines(run, inv)
    sub, tax, total = invoicing.totals(lines, inv.adjustment)
    salaries = sum((dec(x["amount"]) for x in lines if x["source"] == "salary"), dec(0))
    return RunInvoiceOut(
        id=inv.id,
        run_id=run.id,
        number=inv.number,
        issuer_name=settings.in_company_name,
        issuer_address=settings.in_company_address,
        customer_zoho_id=inv.customer_zoho_id,
        customer_name=inv.customer_name,
        customer_address=inv.customer_address,
        currency=inv.currency,
        run_currency=run.currency,
        exchange_rate=inv.exchange_rate,
        needs_rate=missing,
        zoho_rate=rate,
        zoho_rate_note=note,
        rate_from_zoho=bool(rate and inv.exchange_rate and dec(inv.exchange_rate) == dec(rate)),
        invoice_date=inv.invoice_date,
        due_date=inv.due_date,
        payment_terms=inv.payment_terms,
        reference_number=inv.reference_number,
        salary_lines=inv.salary_lines,
        charges=[ChargeIn.model_validate(c) for c in inv.charges or []],
        lines=[InvoiceLineOut.model_validate(x) for x in lines],
        adjustment=inv.adjustment,
        adjustment_description=inv.adjustment_description,
        notes=inv.notes,
        terms=inv.terms,
        salaries=salaries,
        sub_total=sub,
        tax_total=tax,
        total=total,
        zoho_mode=inv.zoho_mode,
        zoho_payload=inv.zoho_payload,
        zoho_invoice_id=inv.zoho_invoice_id,
        zoho_invoice_number=inv.zoho_invoice_number,
        zoho_at=inv.zoho_at,
        zoho_by_name=inv.zoho_by.display_name if inv.zoho_by else None,
        zoho_error=inv.zoho_error,
        report_sent_at=inv.report_sent_at,
        report_to=list(inv.report_to or []),
        report_error=inv.report_error,
    )


# ── the month's invoice ───────────────────────────────────────────────


@router.get("/runs/{run_id}/invoice", response_model=RunInvoiceOut)
async def get_invoice(run_id: uuid.UUID, _: Viewer, session: Session, zoho: Zoho) -> RunInvoiceOut:
    return await _out(session, await _run(session, run_id), zoho)


@router.put("/runs/{run_id}/invoice", response_model=RunInvoiceOut)
async def save_invoice(
    run_id: uuid.UUID, body: RunInvoiceIn, _: Processor, user: CurrentUser, session: Session, zoho: Zoho
) -> RunInvoiceOut:
    run = await _run(session, run_id)
    data = body.model_dump(mode="python")
    # No rate typed for a foreign-currency invoice: Zoho's, for its date.
    if data["currency"].upper() != run.currency and not data.get("exchange_rate"):
        data["exchange_rate"], _ = await zoho_rate(
            zoho, data["currency"].upper(), run.currency, data["invoice_date"]
        )
    try:
        await invoicing.save(session, run, data, user)
    except PayrollError as exc:
        raise _translate(exc) from exc
    return await _out(session, run, zoho)


@router.post("/runs/{run_id}/invoice/zoho", response_model=RunInvoiceOut)
async def invoice_to_zoho(
    run_id: uuid.UUID,
    _: PreparerOrApprover,
    user: CurrentUser,
    session: Session,
    zoho: Zoho,
    mailer: Mailer,
) -> RunInvoiceOut:
    run = await _run(session, run_id)
    try:
        await invoicing.to_zoho(
            session,
            run,
            user,
            zoho=zoho,
            live=app_settings().payroll_zoho_invoice_write,
            mailer=mailer,
            link=month_link(run),
        )
    except PayrollError as exc:
        # Keep what the check found, and the log line saying so: the request
        # fails, but "why" should still be on the invoice afterwards.
        await session.commit()
        raise _translate(exc) from exc
    return await _out(session, run, zoho)


@router.post("/runs/{run_id}/invoice/report", response_model=RunInvoiceOut)
async def invoice_report(
    run_id: uuid.UUID,
    _: PreparerOrApprover,
    user: CurrentUser,
    session: Session,
    mailer: Mailer,
    zoho: Zoho,
) -> RunInvoiceOut:
    run = await _run(session, run_id)
    invoice = await invoicing.get_for_run(session, run)
    if invoice is None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="This month has no invoice yet.")
    try:
        await invoicing.send_report(session, run, invoice, user, mailer=mailer, link=month_link(run))
    except PayrollError as exc:
        raise _translate(exc) from exc
    return await _out(session, run, zoho)


@router.get("/runs/{run_id}/invoice.pdf")
async def invoice_file(run_id: uuid.UUID, _: Viewer, session: Session, zoho: Zoho) -> Response:
    run = await _run(session, run_id)
    settings = await service.get_settings(session)
    saved: PayrollInvoice | None = await invoicing.get_for_run(session, run)
    if saved is not None and saved.zoho_pdf:
        content = saved.zoho_pdf
        name = f"{saved.zoho_invoice_number or saved.number}.pdf"
    else:
        inv: Any = saved or _virtual(run, settings)
        if saved is None:
            inv.number = "Draft"
            inv.exchange_rate, _ = await zoho_rate(zoho, inv.currency, run.currency, inv.invoice_date)
        lines, _ = invoicing.all_lines(run, inv)
        test = inv.zoho_mode != "live"
        content = invoice_pdf(run, inv, lines, settings, test=test)
        name = f"{inv.number}{' TEST' if test else ''}.pdf"
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
    if zoho is None or not zoho.configured:
        error = "Zoho Books is not configured on this server."
    else:
        try:
            for t in await _cached("taxes", zoho.taxes):
                if t.get("tax_id"):
                    taxes.append(
                        ZohoTaxOut(
                            tax_id=str(t["tax_id"]),
                            tax_name=t.get("tax_name") or "Tax",
                            tax_percentage=t.get("tax_percentage") or 0,
                        )
                    )
            currencies = [
                c["currency_code"] for c in await _cached("currencies", zoho.currencies) if c.get("currency_code")
            ]
            orgs = await _cached("org", lambda: zoho._get("/organizations"))
            for o in orgs.get("organizations") or []:
                if str(o.get("organization_id")) == str(zoho.organization_id):
                    org = f"{o.get('name')} ({o.get('currency_code')})"
        except ZohoError as exc:
            error = str(exc)
    approvers = await users_with_roles(session, {"super_admin"})
    report = await invoicing.report_recipients(session)
    currency = settings.invoice_currency or "AED"
    default_rate, default_note = await zoho_rate(zoho, currency, "INR", date.today())
    return InvoiceSetupOut(
        default_currency=currency,
        default_rate=default_rate,
        default_rate_note=default_note,
        zoho_live=app_settings().payroll_zoho_invoice_write,
        zoho_organization=org,
        mail=settings.mail_enabled,
        zoho_error=error,
        taxes=taxes,
        currencies=currencies,
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
