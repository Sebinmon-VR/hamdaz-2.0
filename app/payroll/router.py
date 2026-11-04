"""Payroll's API.

Not gated by a team grant, the same as HR and finance: who may see payroll is
decided in ``app/payroll/access.py``. Every route below names the level it
needs — ``Viewer``, ``Processor``, ``Approver`` or ``Admin`` — in its
signature, except ``/payroll/me``, which answers anyone signed in with what
they may do, so the frontend can show or hide the module without a 403.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import CurrentUser
from app.core.db import get_session
from app.models.payroll import (
    PayrollEmployee,
    PayrollEvent,
    PayrollLine,
    PayrollMember,
    PayrollRun,
    PayrollSalary,
    RunStatus,
)
from app.models.user import User
from app.payroll import exports, service
from app.payroll.access import Access, Admin, Approver, PayrollAccess, Processor, Viewer
from app.payroll.rules import COMPONENTS, Period
from app.payroll.schemas import (
    AccessOverviewOut,
    ComponentOut,
    EmployeeIn,
    EmployeeOut,
    EmployeePatch,
    EventOut,
    GratuityRowOut,
    LineIn,
    LineOut,
    MemberIn,
    MemberOut,
    PaidIn,
    PayrollMeOut,
    PersonOut,
    ReopenIn,
    ReturnIn,
    RoleHolderOut,
    RunCreateIn,
    RunDetailOut,
    RunOut,
    SalaryIn,
    SalaryOut,
    SettingsIn,
    SettingsOut,
    TaxTableIn,
)
from app.payroll.service import PayrollConflict, PayrollError, PayrollNotFound

router = APIRouter(prefix="/payroll", tags=["payroll"])

Session = Annotated[AsyncSession, Depends(get_session)]


def _translate(exc: PayrollError | exports.ExportError) -> HTTPException:
    if isinstance(exc, PayrollNotFound):
        return HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, PayrollConflict):
        return HTTPException(status.HTTP_409_CONFLICT, detail=str(exc))
    return HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc))


def _file(content: bytes | str, name: str, media_type: str) -> Response:
    body = content.encode("utf-8") if isinstance(content, str) else content
    return Response(
        content=body,
        media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"},
    )


# ── who am I ──────────────────────────────────────────────────────────


@router.get("/me", response_model=PayrollMeOut)
async def me(access: Access) -> PayrollMeOut:
    return PayrollMeOut(
        can_view=access.can_view,
        can_process=access.can_process,
        can_approve=access.can_approve,
        can_admin=access.can_admin,
        via=list(access.via),
    )


@router.get("/people", response_model=list[PersonOut])
async def people(_: Viewer, session: Session) -> list[PersonOut]:
    return [
        PersonOut(id=u.id, display_name=u.display_name, email=u.email)
        for u in await service.people(session)
    ]


@router.get("/catalogue", response_model=list[ComponentOut])
async def catalogue(_: Viewer) -> list[ComponentOut]:
    return [
        ComponentOut(
            code=c.code, name=c.name, countries=sorted(c.countries), repeatable=c.repeatable,
            basic=c.basic, pf_wage=c.pf_wage,
        )
        for c in COMPONENTS
    ]


# ── access list ───────────────────────────────────────────────────────


def _member_out(member: PayrollMember) -> MemberOut:
    return MemberOut(
        user_id=member.user_id,
        display_name=member.user.display_name,
        email=member.user.email,
        level=member.level,
        note=member.note,
        added_by_name=None,
        created_at=member.created_at,
    )


@router.get("/access", response_model=AccessOverviewOut)
async def access_overview(_: Viewer, session: Session) -> AccessOverviewOut:
    view = await service.access_overview(session)
    names = {u.id: u.display_name for u in await service.people(session)}
    members = []
    for member in view.members:
        out = _member_out(member)
        out.added_by_name = names.get(member.added_by_id) if member.added_by_id else None
        members.append(out)
    return AccessOverviewOut(
        accounts_team_slug=view.accounts_team_slug,
        accounts_team_found=view.accounts_team_found,
        accounts_members=[
            PersonOut(id=u.id, display_name=u.display_name, email=u.email)
            for u in view.accounts_members
        ],
        role_holders=[
            RoleHolderOut(
                person=PersonOut(id=u.id, display_name=u.display_name, email=u.email),
                roles=sorted(keys),
            )
            for u, keys in view.role_holders
        ],
        members=members,
    )


@router.put("/access/{user_id}", response_model=MemberOut)
async def set_member(
    user_id: uuid.UUID, body: MemberIn, _: Admin, user: CurrentUser, session: Session
) -> MemberOut:
    try:
        member = await service.set_member(session, user_id, body.level, body.note, user)
    except PayrollError as exc:
        raise _translate(exc) from exc
    return _member_out(member)


@router.delete("/access/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(user_id: uuid.UUID, _: Admin, user: CurrentUser, session: Session) -> None:
    try:
        await service.remove_member(session, user_id, user)
    except PayrollError as exc:
        raise _translate(exc) from exc


# ── settings ──────────────────────────────────────────────────────────


@router.get("/settings", response_model=SettingsOut)
async def get_settings(_: Viewer, session: Session) -> SettingsOut:
    return SettingsOut.model_validate(await service.get_settings(session))


@router.put("/settings", response_model=SettingsOut)
async def put_settings(
    body: SettingsIn, _: Processor, user: CurrentUser, session: Session
) -> SettingsOut:
    try:
        settings = await service.update_settings(session, body.model_dump(exclude_unset=True), user)
    except PayrollError as exc:
        raise _translate(exc) from exc
    return SettingsOut.model_validate(settings)


@router.put("/settings/tax-tables/{fy}", response_model=SettingsOut)
async def put_tax_table(
    fy: int, body: TaxTableIn, _: Processor, user: CurrentUser, session: Session
) -> SettingsOut:
    if not 2000 <= fy <= 2100:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="That financial year is out of range.")
    data = body.model_dump(exclude={"confirm"})
    try:
        settings = await service.save_tax_table(session, fy, data, confirm=body.confirm, actor=user)
    except PayrollError as exc:
        raise _translate(exc) from exc
    return SettingsOut.model_validate(settings)


# ── employees ─────────────────────────────────────────────────────────


def _salary_out(salary: PayrollSalary) -> SalaryOut:
    return SalaryOut(
        id=salary.id,
        effective_from=salary.effective_from,
        components=salary.components,
        monthly_total=service.monthly_total(salary),
        note=salary.note,
        created_at=salary.created_at,
    )


async def _employee_out(session: AsyncSession, employee: PayrollEmployee) -> EmployeeOut:
    current = service.salary_on(employee, date.today())
    if current is None and employee.salaries:
        # Joining in the future: show the first structure rather than none.
        current = sorted(employee.salaries, key=lambda s: s.effective_from)[0]
    user_name = None
    if employee.user_id:
        linked = await session.get(User, employee.user_id)
        user_name = linked.display_name if linked else None
    return EmployeeOut(
        id=employee.id,
        country=employee.country,
        currency=employee.currency,
        active=employee.left_on is None or employee.left_on >= date.today(),
        user_name=user_name,
        current_salary=_salary_out(current) if current else None,
        salaries=[_salary_out(s) for s in sorted(employee.salaries, key=lambda s: s.effective_from, reverse=True)],
        created_at=employee.created_at,
        updated_at=employee.updated_at,
        user_id=employee.user_id,
        code=employee.code,
        full_name=employee.full_name,
        email=employee.email,
        designation=employee.designation,
        department=employee.department,
        joined_on=employee.joined_on,
        left_on=employee.left_on,
        bank_name=employee.bank_name,
        bank_account=employee.bank_account,
        iban=employee.iban,
        bank_routing=employee.bank_routing,
        mol_person_id=employee.mol_person_id,
        ifsc=employee.ifsc,
        pan=employee.pan,
        uan=employee.uan,
        esi_number=employee.esi_number,
        pt_state=employee.pt_state,
        pf_enabled=employee.pf_enabled,
        pf_on_actual=employee.pf_on_actual,
        eps_member=employee.eps_member,
        esi_mode=employee.esi_mode,  # type: ignore[arg-type]
        pt_override=employee.pt_override,
        tds_monthly_override=employee.tds_monthly_override,
        opening_fy=employee.opening_fy,
        opening_taxable=employee.opening_taxable,
        opening_tds=employee.opening_tds,
        notes=employee.notes,
    )


@router.get("/employees", response_model=list[EmployeeOut])
async def list_employees(
    _: Viewer,
    session: Session,
    country: Annotated[str | None, Query(pattern="^(AE|IN)$")] = None,
    include_left: bool = True,
) -> list[EmployeeOut]:
    rows = await service.list_employees(session, country=country, include_left=include_left)
    return [await _employee_out(session, e) for e in rows]


@router.post("/employees", response_model=EmployeeOut, status_code=status.HTTP_201_CREATED)
async def create_employee(
    body: EmployeeIn, _: Processor, user: CurrentUser, session: Session
) -> EmployeeOut:
    data = body.model_dump()
    if body.salary is None:
        data.pop("salary", None)
    try:
        employee = await service.create_employee(session, data, user)
    except PayrollError as exc:
        raise _translate(exc) from exc
    return await _employee_out(session, employee)


@router.get("/employees/{employee_id}", response_model=EmployeeOut)
async def get_employee(employee_id: uuid.UUID, _: Viewer, session: Session) -> EmployeeOut:
    try:
        employee = await service.get_employee(session, employee_id)
    except PayrollError as exc:
        raise _translate(exc) from exc
    return await _employee_out(session, employee)


@router.patch("/employees/{employee_id}", response_model=EmployeeOut)
async def patch_employee(
    employee_id: uuid.UUID, body: EmployeePatch, _: Processor, user: CurrentUser, session: Session
) -> EmployeeOut:
    try:
        employee = await service.get_employee(session, employee_id)
        employee = await service.update_employee(
            session, employee, body.model_dump(exclude_unset=True), user
        )
    except PayrollError as exc:
        raise _translate(exc) from exc
    return await _employee_out(session, employee)


@router.delete("/employees/{employee_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_employee(
    employee_id: uuid.UUID, _: Processor, user: CurrentUser, session: Session
) -> None:
    try:
        employee = await service.get_employee(session, employee_id)
        await service.delete_employee(session, employee, user)
    except PayrollError as exc:
        raise _translate(exc) from exc


@router.post("/employees/{employee_id}/salaries", response_model=EmployeeOut)
async def add_salary(
    employee_id: uuid.UUID, body: SalaryIn, _: Processor, user: CurrentUser, session: Session
) -> EmployeeOut:
    try:
        employee = await service.get_employee(session, employee_id)
        await service.add_salary(session, employee, body.model_dump(), user)
    except PayrollError as exc:
        raise _translate(exc) from exc
    return await _employee_out(session, employee)


@router.delete("/employees/{employee_id}/salaries/{salary_id}", response_model=EmployeeOut)
async def delete_salary(
    employee_id: uuid.UUID, salary_id: uuid.UUID, _: Processor, user: CurrentUser, session: Session
) -> EmployeeOut:
    try:
        employee = await service.get_employee(session, employee_id)
        await service.delete_salary(session, employee, salary_id, user)
    except PayrollError as exc:
        raise _translate(exc) from exc
    return await _employee_out(session, employee)


@router.get("/employees/{employee_id}/events", response_model=list[EventOut])
async def employee_events(employee_id: uuid.UUID, _: Viewer, session: Session) -> list[EventOut]:
    return [_event_out(e) for e in await service.events(session, employee_id=employee_id)]


@router.get("/gratuity", response_model=list[GratuityRowOut])
async def gratuity(
    _: Viewer, session: Session, as_of: date | None = None
) -> list[GratuityRowOut]:
    return [GratuityRowOut(**row) for row in await service.gratuity(session, as_of or date.today())]


# ── runs ──────────────────────────────────────────────────────────────


def _run_out(run: PayrollRun) -> RunOut:
    line_warnings = sum(len(line.warnings or []) for line in run.lines)
    return RunOut(
        id=run.id,
        country=run.country,
        year=run.year,
        month=run.month,
        label=Period(run.year, run.month).label,
        currency=run.currency,
        status=run.status,
        headcount=run.headcount,
        gross=run.gross,
        deductions=run.deductions,
        net=run.net,
        employer_cost=run.employer_cost,
        warnings=list(run.warnings or []),
        warning_count=len(run.warnings or []) + line_warnings,
        note=run.note,
        calculated_at=run.calculated_at,
        created_by_name=run.created_by.display_name if run.created_by else None,
        submitted_by_id=run.submitted_by_id,
        submitted_by_name=run.submitted_by.display_name if run.submitted_by else None,
        submitted_at=run.submitted_at,
        approved_by_name=run.approved_by.display_name if run.approved_by else None,
        approved_at=run.approved_at,
        returned_note=run.returned_note,
        paid_by_name=run.paid_by.display_name if run.paid_by else None,
        paid_on=run.paid_on,
        created_at=run.created_at,
    )


def _line_out(line: PayrollLine) -> LineOut:
    return LineOut(
        id=line.id,
        employee_id=line.employee_id,
        employee_code=line.employee_code,
        employee_name=line.employee_name,
        designation=line.designation,
        identifiers=line.identifiers or {},
        lop_adjust=line.lop_adjust,
        ot_normal_hours=line.ot_normal_hours,
        ot_premium_hours=line.ot_premium_hours,
        extras=line.extras or [],
        tds_override=line.tds_override,
        on_hold=line.on_hold,
        note=line.note,
        leave_lop_days=line.leave_lop_days,
        basis_days=line.basis_days,
        paid_days=line.paid_days,
        unpaid_days=line.unpaid_days,
        earnings=line.earnings or [],
        deductions=line.deductions or [],
        employer=line.employer or [],
        gross=line.gross,
        total_deductions=line.total_deductions,
        net=line.net,
        employer_cost=line.employer_cost,
        taxable=line.taxable,
        tds=line.tds,
        workings=line.workings or {},
        warnings=list(line.warnings or []),
    )


def _event_out(event: PayrollEvent) -> EventOut:
    return EventOut(
        id=event.id,
        action=event.action,
        summary=event.summary,
        detail=event.detail or {},
        actor_name=event.actor.display_name if event.actor else None,
        run_id=event.run_id,
        employee_id=event.employee_id,
        invoice_id=event.invoice_id,
        created_at=event.created_at,
    )


async def _detail(session: AsyncSession, run: PayrollRun) -> RunDetailOut:
    base = _run_out(run)
    return RunDetailOut(
        **base.model_dump(),
        lines=[_line_out(line) for line in sorted(run.lines, key=lambda ln: ln.employee_code)],
        events=[_event_out(e) for e in await service.events(session, run_id=run.id)],
    )


async def _run(session: AsyncSession, run_id: uuid.UUID) -> PayrollRun:
    try:
        return await service.get_run(session, run_id)
    except PayrollError as exc:
        raise _translate(exc) from exc


@router.get("/runs", response_model=list[RunOut])
async def list_runs(
    _: Viewer,
    session: Session,
    country: Annotated[str | None, Query(pattern="^(AE|IN)$")] = None,
    year: int | None = None,
) -> list[RunOut]:
    return [_run_out(r) for r in await service.list_runs(session, country=country, year=year)]


@router.post("/runs", response_model=RunDetailOut, status_code=status.HTTP_201_CREATED)
async def create_run(
    body: RunCreateIn, _: Processor, user: CurrentUser, session: Session
) -> RunDetailOut:
    try:
        run = await service.create_run(
            session, country=body.country, year=body.year, month=body.month, note=body.note, actor=user
        )
    except PayrollError as exc:
        raise _translate(exc) from exc
    return await _detail(session, run)


@router.get("/runs/{run_id}", response_model=RunDetailOut)
async def get_run(run_id: uuid.UUID, _: Viewer, session: Session) -> RunDetailOut:
    return await _detail(session, await _run(session, run_id))


@router.delete("/runs/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_run(run_id: uuid.UUID, _: Processor, user: CurrentUser, session: Session) -> None:
    run = await _run(session, run_id)
    try:
        await service.delete_run(session, run, user)
    except PayrollError as exc:
        raise _translate(exc) from exc


@router.post("/runs/{run_id}/recalculate", response_model=RunDetailOut)
async def recalculate(
    run_id: uuid.UUID, _: Processor, user: CurrentUser, session: Session
) -> RunDetailOut:
    run = await _run(session, run_id)
    try:
        await service.recalculate(session, run, user)
    except PayrollError as exc:
        raise _translate(exc) from exc
    return await _detail(session, run)


@router.patch("/runs/{run_id}/lines/{line_id}", response_model=RunDetailOut)
async def update_line(
    run_id: uuid.UUID,
    line_id: uuid.UUID,
    body: LineIn,
    _: Processor,
    user: CurrentUser,
    session: Session,
) -> RunDetailOut:
    run = await _run(session, run_id)
    try:
        await service.update_line(session, run, line_id, body.model_dump(exclude_unset=True), user)
    except PayrollError as exc:
        raise _translate(exc) from exc
    return await _detail(session, run)


@router.post("/runs/{run_id}/submit", response_model=RunDetailOut)
async def submit(run_id: uuid.UUID, _: Processor, user: CurrentUser, session: Session) -> RunDetailOut:
    run = await _run(session, run_id)
    try:
        await service.submit(session, run, user)
    except PayrollError as exc:
        raise _translate(exc) from exc
    return await _detail(session, run)


@router.post("/runs/{run_id}/approve", response_model=RunDetailOut)
async def approve(
    run_id: uuid.UUID, access: Approver, user: CurrentUser, session: Session
) -> RunDetailOut:
    run = await _run(session, run_id)
    try:
        await service.approve(session, run, user, own_allowed=access.can_admin)
    except PayrollError as exc:
        raise _translate(exc) from exc
    return await _detail(session, run)


@router.post("/runs/{run_id}/return", response_model=RunDetailOut)
async def return_run(
    run_id: uuid.UUID, body: ReturnIn, _: Approver, user: CurrentUser, session: Session
) -> RunDetailOut:
    run = await _run(session, run_id)
    try:
        await service.return_run(session, run, body.note, user)
    except PayrollError as exc:
        raise _translate(exc) from exc
    return await _detail(session, run)


@router.post("/runs/{run_id}/paid", response_model=RunDetailOut)
async def mark_paid(
    run_id: uuid.UUID, body: PaidIn, _: Processor, user: CurrentUser, session: Session
) -> RunDetailOut:
    run = await _run(session, run_id)
    try:
        await service.mark_paid(session, run, body.paid_on, user)
    except PayrollError as exc:
        raise _translate(exc) from exc
    return await _detail(session, run)


@router.post("/runs/{run_id}/reopen", response_model=RunDetailOut)
async def reopen(
    run_id: uuid.UUID, body: ReopenIn, _: Admin, user: CurrentUser, session: Session
) -> RunDetailOut:
    run = await _run(session, run_id)
    try:
        await service.reopen(session, run, body.reason, user)
    except PayrollError as exc:
        raise _translate(exc) from exc
    return await _detail(session, run)


# ── files ─────────────────────────────────────────────────────────────


@router.get("/runs/{run_id}/payslips.pdf")
async def payslips(
    run_id: uuid.UUID,
    _: Viewer,
    session: Session,
    line: uuid.UUID | None = None,
) -> Response:
    run = await _run(session, run_id)
    lines = sorted(run.lines, key=lambda ln: ln.employee_code)
    stem = exports.file_stem(run)
    if line is not None:
        lines = [ln for ln in lines if ln.id == line]
        if not lines:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No such line in this month.")
        stem = f"Payslip {lines[0].employee_code} {run.year}-{run.month:02d}"
    pdf = exports.payslips_pdf(run, lines, await service.get_settings(session))
    return _file(pdf, f"{stem}.pdf", exports.PDF_TYPE)


@router.get("/runs/{run_id}/register.xlsx")
async def register(run_id: uuid.UUID, _: Viewer, session: Session) -> Response:
    run = await _run(session, run_id)
    data = exports.register_xlsx(run, await service.get_settings(session))
    return _file(data, f"{exports.file_stem(run)} register.xlsx", exports.XLSX_TYPE)


def _bank_gate(run: PayrollRun, access: PayrollAccess) -> None:
    if not (access.can_process or access.can_approve):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail="Bank files are for payroll processors and approvers.",
        )
    if run.status not in (RunStatus.APPROVED, RunStatus.PAID):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="Bank files are only produced once the month is approved.",
        )


@router.get("/runs/{run_id}/wps.sif")
async def wps(run_id: uuid.UUID, access: Viewer, user: CurrentUser, session: Session) -> Response:
    run = await _run(session, run_id)
    _bank_gate(run, access)
    try:
        name, content = exports.wps_sif(run, await service.get_settings(session))
    except exports.ExportError as exc:
        raise _translate(exc) from exc
    await service.log(session, user, "file.wps", f"Downloaded the WPS file {name}", run_id=run.id)
    return _file(content, name, "text/plain")


@router.get("/runs/{run_id}/bank.csv")
async def bank(run_id: uuid.UUID, access: Viewer, user: CurrentUser, session: Session) -> Response:
    run = await _run(session, run_id)
    _bank_gate(run, access)
    try:
        content = exports.bank_csv(run)
    except exports.ExportError as exc:
        raise _translate(exc) from exc
    await service.log(session, user, "file.bank", "Downloaded the bank transfer sheet", run_id=run.id)
    return _file(content, f"{exports.file_stem(run)} bank.csv", "text/csv")


@router.get("/runs/{run_id}/pf-ecr.txt")
async def pf_ecr(run_id: uuid.UUID, access: Viewer, user: CurrentUser, session: Session) -> Response:
    run = await _run(session, run_id)
    _bank_gate(run, access)
    try:
        content = exports.pf_ecr(run)
    except exports.ExportError as exc:
        raise _translate(exc) from exc
    await service.log(session, user, "file.ecr", "Downloaded the PF ECR file", run_id=run.id)
    return _file(content, f"{exports.file_stem(run)} PF ECR.txt", "text/plain")


@router.get("/events", response_model=list[EventOut])
async def all_events(
    _: Viewer, session: Session, limit: Annotated[int, Query(ge=1, le=500)] = 200
) -> list[EventOut]:
    return [_event_out(e) for e in await service.events(session, limit=limit)]
