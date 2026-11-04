"""Payroll's database side: employees, salaries, runs, and the steps a month goes through.

The arithmetic is not here — ``app/payroll/rules.py`` does all of it. This
module gathers what the rules need (the structure in force, approved unpaid
leave, what was paid earlier in the financial year), stores what they return,
and moves a month through draft → submitted → approved → paid.

Every write leaves a ``PayrollEvent``. Payroll is the kind of record people
are asked about months later, and "who changed this salary, and when" should
be a query rather than a recollection.
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Iterable

from sqlalchemy import and_, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.leave import LeaveRequest, LeaveStatus, LeaveType
from app.models.notification import NotificationKind
from app.models.payroll import (
    CURRENCY,
    AccessLevel,
    Country,
    PayrollEmployee,
    PayrollEvent,
    PayrollLine,
    PayrollMember,
    PayrollRun,
    PayrollSalary,
    PayrollSettings,
    RunStatus,
)
from app.models.role import Role, UserRole
from app.models.team import Team
from app.models.user import User
from app.payroll import access as payroll_access
from app.payroll import rules
from app.payroll.rules import (
    DEFAULT_TAX_TABLES,
    EmployeeFacts,
    IndiaRules,
    Inputs,
    Period,
    RuleError,
    TaxSoFar,
    TaxTable,
    UaeRules,
    dec,
    fy_label,
)


class PayrollError(Exception):
    """A request that cannot be done, in words a processor can act on."""


class PayrollNotFound(PayrollError):
    pass


class PayrollConflict(PayrollError):
    pass


# ── the log ───────────────────────────────────────────────────────────


async def log(
    session: AsyncSession,
    actor: User | None,
    action: str,
    summary: str,
    *,
    run_id: uuid.UUID | None = None,
    employee_id: uuid.UUID | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    session.add(
        PayrollEvent(
            actor_id=actor.id if actor else None,
            actor=actor,
            action=action,
            summary=summary[:2000],
            run_id=run_id,
            employee_id=employee_id,
            detail=rules.to_json(detail or {}),
        )
    )


async def events(
    session: AsyncSession,
    *,
    run_id: uuid.UUID | None = None,
    employee_id: uuid.UUID | None = None,
    limit: int = 200,
) -> list[PayrollEvent]:
    query = select(PayrollEvent).order_by(PayrollEvent.created_at.desc()).limit(limit)
    if run_id is not None:
        query = query.where(PayrollEvent.run_id == run_id)
    if employee_id is not None:
        query = query.where(PayrollEvent.employee_id == employee_id)
    return list((await session.scalars(query)).unique().all())


# ── settings ──────────────────────────────────────────────────────────


async def get_settings(session: AsyncSession) -> PayrollSettings:
    settings = await session.get(PayrollSettings, 1)
    if settings is None:
        # The UAE letterhead is the one quotes already carry; India's entity
        # is not known here, so a processor names it in settings.
        from app.quoting.report_pdf import ADDRESS, ORG

        settings = PayrollSettings(
            id=1,
            ae_company_name=ORG,
            ae_company_address="\n".join(ADDRESS[:2]),
            in_tax_tables=dict(DEFAULT_TAX_TABLES),
            in_pt_slabs={},
        )
        session.add(settings)
        await session.flush()
    return settings


def rules_from(settings: PayrollSettings) -> tuple[UaeRules, IndiaRules]:
    uae = UaeRules(
        day_basis=settings.ae_day_basis,
        hours_per_day=dec(settings.ae_hours_per_day),
        ot_normal_rate=dec(settings.ae_ot_normal_rate),
        ot_premium_rate=dec(settings.ae_ot_premium_rate),
    )
    india = IndiaRules(
        day_basis=settings.in_day_basis,
        pf_ee_rate=dec(settings.in_pf_ee_rate),
        pf_er_rate=dec(settings.in_pf_er_rate),
        eps_rate=dec(settings.in_eps_rate),
        pf_ceiling=dec(settings.in_pf_ceiling),
        edli_rate=dec(settings.in_edli_rate),
        pf_admin_rate=dec(settings.in_pf_admin_rate),
        wage_code_rule=settings.in_wage_code_rule,
        esi_threshold=dec(settings.in_esi_threshold),
        esi_ee_rate=dec(settings.in_esi_ee_rate),
        esi_er_rate=dec(settings.in_esi_er_rate),
        pt_slabs=rules.pt_slabs_from_json(settings.in_pt_slabs),
    )
    return uae, india


async def update_settings(
    session: AsyncSession, changes: dict[str, Any], actor: User
) -> PayrollSettings:
    settings = await get_settings(session)
    if "accounts_team_slug" in changes and changes["accounts_team_slug"]:
        from app.teams import service as teams_service
        from app.teams.service import TeamError

        slug = changes["accounts_team_slug"].strip()
        try:
            await teams_service.get_team(session, slug)
        except TeamError as exc:
            raise PayrollError(f"There is no team {slug!r}.") from exc
        changes["accounts_team_slug"] = slug
    if "in_pt_slabs" in changes and changes["in_pt_slabs"] is not None:
        try:
            changes["in_pt_slabs"] = rules.clean_pt_slabs(changes["in_pt_slabs"])
        except RuleError as exc:
            raise PayrollError(str(exc)) from exc
    for digits, key, label in (
        (13, "ae_employer_mol_id", "The employer MOHRE id"),
        (9, "ae_employer_bank_routing", "The employer bank routing code"),
    ):
        if changes.get(key):
            value = re.sub(r"\s+", "", changes[key])
            if not value.isdigit() or len(value) != digits:
                raise PayrollError(f"{label} must be {digits} digits.")
            changes[key] = value

    before: dict[str, Any] = {}
    for key, value in changes.items():
        if value is None and key not in {
            "ae_company_address", "in_company_address", "ae_employer_mol_id",
            "ae_employer_bank_routing", "ae_employer_reference", "in_pf_establishment",
        }:
            continue
        if not hasattr(settings, key):
            continue
        old = getattr(settings, key)
        if old != value:
            before[key] = {"from": old, "to": value}
            setattr(settings, key, value)
    if before:
        settings.updated_by_id = actor.id
        await log(
            session, actor, "settings.updated",
            "Changed payroll settings: " + ", ".join(sorted(before)),
            detail=before,
        )
    await session.flush()
    return settings


async def save_tax_table(
    session: AsyncSession, fy: int, data: dict[str, Any], *, confirm: bool, actor: User
) -> PayrollSettings:
    settings = await get_settings(session)
    raw = dict(data)
    raw["confirmed"] = confirm
    raw["confirmed_by"] = actor.display_name if confirm else None
    raw["confirmed_at"] = datetime.now(UTC).isoformat() if confirm else None
    try:
        table = TaxTable.from_json(fy, raw)
    except (RuleError, KeyError, ArithmeticError, TypeError) as exc:
        raise PayrollError(f"That tax table cannot be used: {exc}") from exc
    tables = dict(settings.in_tax_tables or {})
    tables[str(fy)] = table.to_json()
    settings.in_tax_tables = tables
    settings.updated_by_id = actor.id
    await log(
        session, actor, "tax_table.saved",
        f"{'Confirmed' if confirm else 'Saved'} the FY {fy_label(fy)} income tax table",
        detail={"fy": fy, "table": table.to_json()},
    )
    await session.flush()
    return settings


# ── access list ───────────────────────────────────────────────────────


@dataclass
class AccessOverview:
    accounts_team_slug: str
    accounts_team_found: bool
    accounts_members: list[User]
    role_holders: list[tuple[User, list[str]]]
    members: list[PayrollMember]


async def access_overview(session: AsyncSession) -> AccessOverview:
    slug = await payroll_access.accounts_slug(session)
    found = await session.scalar(select(Team.id).where(Team.slug == slug)) is not None
    ids = await payroll_access.accounts_member_ids(session)
    accounts = (
        list((await session.scalars(select(User).where(User.id.in_(ids)))).all()) if ids else []
    )
    rows = (
        await session.execute(
            select(User, Role.key)
            .join(UserRole, UserRole.user_id == User.id)
            .join(Role, Role.id == UserRole.role_id)
            .where(Role.key.in_(payroll_access.VIEW_ROLES | payroll_access.APPROVE_ROLES))
        )
    ).all()
    holders: dict[uuid.UUID, tuple[User, list[str]]] = {}
    for user, key in rows:
        holders.setdefault(user.id, (user, []))[1].append(key)
    members = list(
        (await session.scalars(select(PayrollMember).order_by(PayrollMember.created_at))).all()
    )
    return AccessOverview(
        accounts_team_slug=slug,
        accounts_team_found=found,
        accounts_members=sorted(accounts, key=lambda u: u.display_name.casefold()),
        role_holders=sorted(holders.values(), key=lambda h: h[0].display_name.casefold()),
        members=members,
    )


async def set_member(
    session: AsyncSession, user_id: uuid.UUID, level: str, note: str | None, actor: User
) -> PayrollMember:
    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise PayrollNotFound("No active user with that id.")
    member = await session.scalar(select(PayrollMember).where(PayrollMember.user_id == user_id))
    level_name = "processor" if level == AccessLevel.PROCESSOR else "viewer"
    if member is None:
        member = PayrollMember(
            user_id=user.id, user=user, level=AccessLevel(level), note=note, added_by_id=actor.id
        )
        session.add(member)
        summary = f"Gave {user.display_name} payroll access as {level_name}"
    else:
        if member.level == level and member.note == note:
            return member
        summary = f"Changed {user.display_name}'s payroll access to {level_name}"
        member.level = AccessLevel(level)
        member.note = note
    await log(session, actor, "access.granted", summary, detail={"user": str(user.id), "level": level})
    await session.flush()
    return member


async def remove_member(session: AsyncSession, user_id: uuid.UUID, actor: User) -> None:
    member = await session.scalar(select(PayrollMember).where(PayrollMember.user_id == user_id))
    if member is None:
        raise PayrollNotFound("That person is not on the payroll access list.")
    name = member.user.display_name
    await session.delete(member)
    await log(
        session, actor, "access.removed", f"Removed {name} from the payroll access list",
        detail={"user": str(user_id)},
    )
    await session.flush()


async def people(session: AsyncSession) -> list[User]:
    """Everyone with a login, for linking an employee or granting access."""
    return list(
        (
            await session.scalars(
                select(User).where(User.is_active.is_(True)).order_by(User.display_name)
            )
        ).all()
    )


# ── employees ─────────────────────────────────────────────────────────

_IFSC = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
_PAN = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")

_CLEARABLE = {
    "user_id", "email", "designation", "department", "left_on", "bank_name", "bank_account",
    "iban", "bank_routing", "mol_person_id", "ifsc", "pan", "uan", "esi_number", "pt_state",
    "pt_override", "tds_monthly_override", "opening_fy", "notes",
}


def _normalise(country: str, values: dict[str, Any]) -> dict[str, Any]:
    """Tidy identifiers and refuse ones a bank or the PF portal would reject."""
    out = dict(values)
    for key in ("iban", "ifsc", "pan", "pt_state", "bank_account", "bank_routing",
                "mol_person_id", "uan", "esi_number"):
        if key in out and isinstance(out[key], str):
            cleaned = re.sub(r"\s+", "", out[key]).upper()
            out[key] = cleaned or None
    for key in ("code", "full_name", "email", "designation", "department", "bank_name"):
        if key in out and isinstance(out[key], str):
            out[key] = out[key].strip() or None
    if out.get("email"):
        out["email"] = out["email"].lower()
    for key in ("pt_override", "tds_monthly_override", "opening_taxable", "opening_tds"):
        if out.get(key) is not None:
            out[key] = rules.r2(out[key])

    if country == Country.UAE:
        iban = out.get("iban")
        if iban and not (len(iban) == 23 and iban.startswith("AE") and iban[2:].isdigit()):
            raise PayrollError("A UAE IBAN is AE followed by 21 digits (23 characters).")
        for key, digits, label in (
            ("bank_routing", 9, "The bank routing code"),
            ("mol_person_id", 14, "The MOHRE person id"),
        ):
            value = out.get(key)
            if value and (not value.isdigit() or len(value) != digits):
                raise PayrollError(f"{label} must be {digits} digits.")
    else:
        if out.get("ifsc") and not _IFSC.match(out["ifsc"]):
            raise PayrollError("An IFSC is 11 characters: four letters, a zero, then six more.")
        if out.get("pan") and not _PAN.match(out["pan"]):
            raise PayrollError("A PAN is five letters, four digits and a letter (ABCDE1234F).")
        if out.get("uan") and (not out["uan"].isdigit() or len(out["uan"]) != 12):
            raise PayrollError("A UAN is 12 digits.")
        if out.get("bank_account") and not out["bank_account"].isdigit():
            raise PayrollError("An Indian bank account number is digits only.")
    return out


async def _check_unique(
    session: AsyncSession,
    *,
    code: str | None,
    user_id: uuid.UUID | None,
    exclude: uuid.UUID | None = None,
) -> None:
    other = PayrollEmployee.id != exclude if exclude else true()
    if code:
        clash = await session.scalar(
            select(PayrollEmployee).where(
                func.lower(PayrollEmployee.code) == code.lower(), other
            )
        )
        if clash is not None:
            raise PayrollConflict(f"Employee code {code} is already {clash.full_name}'s.")
    if user_id:
        if await session.get(User, user_id) is None:
            raise PayrollNotFound("The login to link does not exist.")
        clash = await session.scalar(
            select(PayrollEmployee).where(PayrollEmployee.user_id == user_id, other)
        )
        if clash is not None:
            raise PayrollConflict(f"That login is already linked to {clash.full_name}.")


async def list_employees(
    session: AsyncSession, *, country: str | None = None, include_left: bool = True
) -> list[PayrollEmployee]:
    query = select(PayrollEmployee).order_by(PayrollEmployee.code)
    if country:
        query = query.where(PayrollEmployee.country == country)
    if not include_left:
        query = query.where(
            or_(PayrollEmployee.left_on.is_(None), PayrollEmployee.left_on >= date.today())
        )
    return list((await session.scalars(query)).all())


async def get_employee(session: AsyncSession, employee_id: uuid.UUID) -> PayrollEmployee:
    employee = await session.get(PayrollEmployee, employee_id)
    if employee is None:
        raise PayrollNotFound("No such employee on payroll.")
    return employee


def salary_on(employee: PayrollEmployee, day: date) -> PayrollSalary | None:
    current = None
    for salary in sorted(employee.salaries, key=lambda s: s.effective_from):
        if salary.effective_from <= day:
            current = salary
    return current


def monthly_total(salary: PayrollSalary) -> Decimal:
    return rules.r2(sum((dec(c["amount"]) for c in salary.components), Decimal("0")))


async def create_employee(
    session: AsyncSession, data: dict[str, Any], actor: User
) -> PayrollEmployee:
    country = data.pop("country")
    salary = data.pop("salary", None)
    values = _normalise(country, data)
    if values.get("left_on") and values["left_on"] < values["joined_on"]:
        raise PayrollError("The leaving date is before the joining date.")
    await _check_unique(session, code=values["code"], user_id=values.get("user_id"))
    employee = PayrollEmployee(country=Country(country), created_by_id=actor.id, salaries=[], **values)
    if salary:
        employee.salaries = [_new_salary(country, salary, actor)]
    session.add(employee)
    await session.flush()
    await log(
        session, actor, "employee.created",
        f"Added {employee.full_name} ({employee.code}) to {_country(country)} payroll",
        employee_id=employee.id,
    )
    return employee


async def update_employee(
    session: AsyncSession, employee: PayrollEmployee, changes: dict[str, Any], actor: User
) -> PayrollEmployee:
    values = _normalise(employee.country, changes)
    if "code" in values or "user_id" in values:
        await _check_unique(
            session,
            code=values.get("code"),
            user_id=values.get("user_id"),
            exclude=employee.id,
        )
    changed: dict[str, Any] = {}
    for key, value in values.items():
        if value is None and key not in _CLEARABLE:
            continue
        old = getattr(employee, key)
        if old != value:
            # Bank details are logged as changed, not with their values: the
            # log is readable by every payroll viewer.
            sensitive = key in {"bank_account", "iban", "ifsc", "pan", "uan", "esi_number"}
            changed[key] = "changed" if sensitive else {"from": old, "to": value}
            setattr(employee, key, value)
    joined, left = employee.joined_on, employee.left_on
    if left and left < joined:
        raise PayrollError("The leaving date is before the joining date.")
    if changed:
        employee.updated_by_id = actor.id
        await log(
            session, actor, "employee.updated",
            f"Changed {employee.full_name}: " + ", ".join(sorted(changed)),
            employee_id=employee.id, detail=changed,
        )
    await session.flush()
    return employee


async def delete_employee(session: AsyncSession, employee: PayrollEmployee, actor: User) -> None:
    paid = await session.scalar(
        select(func.count())
        .select_from(PayrollLine)
        .join(PayrollRun, PayrollRun.id == PayrollLine.run_id)
        .where(PayrollLine.employee_id == employee.id, PayrollRun.status != RunStatus.DRAFT)
    )
    if paid:
        raise PayrollConflict(
            f"{employee.full_name} is in {paid} submitted or approved month(s) and cannot be "
            f"deleted. Set a leaving date instead."
        )
    drafts = (
        await session.scalars(select(PayrollLine).where(PayrollLine.employee_id == employee.id))
    ).all()
    run_ids = {line.run_id for line in drafts}
    for line in drafts:
        await session.delete(line)
    await session.flush()
    for run_id in run_ids:
        run = await session.get(PayrollRun, run_id)
        if run is not None:
            await session.refresh(run, ["lines"])
            _totals(run)
    await log(
        session, actor, "employee.deleted",
        f"Deleted {employee.full_name} ({employee.code}) from payroll",
        detail={"code": employee.code, "name": employee.full_name},
    )
    await session.delete(employee)
    await session.flush()


def _new_salary(country: str, data: dict[str, Any], actor: User) -> PayrollSalary:
    try:
        components = rules.clean_components(country, data.get("components") or [])
    except RuleError as exc:
        raise PayrollError(str(exc)) from exc
    return PayrollSalary(
        effective_from=data["effective_from"],
        components=components,
        note=(data.get("note") or None),
        created_by_id=actor.id,
    )


async def add_salary(
    session: AsyncSession, employee: PayrollEmployee, data: dict[str, Any], actor: User
) -> PayrollSalary:
    if any(s.effective_from == data["effective_from"] for s in employee.salaries):
        raise PayrollConflict(
            f"There is already a salary from {data['effective_from'].isoformat()}. "
            f"Delete it first, or pick another date."
        )
    if data["effective_from"] < employee.joined_on:
        raise PayrollError("A salary cannot start before the joining date.")
    salary = _new_salary(employee.country, data, actor)
    salary.employee_id = employee.id
    employee.salaries.append(salary)
    await session.flush()
    await log(
        session, actor, "salary.added",
        f"New salary for {employee.full_name} from {salary.effective_from.isoformat()}: "
        f"{employee.currency} {monthly_total(salary)} a month",
        employee_id=employee.id,
        detail={"effective_from": salary.effective_from, "components": salary.components},
    )
    return salary


async def delete_salary(
    session: AsyncSession, employee: PayrollEmployee, salary_id: uuid.UUID, actor: User
) -> None:
    salary = next((s for s in employee.salaries if s.id == salary_id), None)
    if salary is None:
        raise PayrollNotFound("No such salary on this employee.")
    employee.salaries.remove(salary)
    await log(
        session, actor, "salary.deleted",
        f"Deleted {employee.full_name}'s salary from {salary.effective_from.isoformat()}",
        employee_id=employee.id,
        detail={"effective_from": salary.effective_from, "components": salary.components},
    )
    await session.flush()


def _country(code: str) -> str:
    return "UAE" if code == Country.UAE else "India"


async def gratuity(session: AsyncSession, as_of: date) -> list[dict[str, Any]]:
    """UAE end-of-service gratuity owed to each employee on ``as_of``."""
    rows: list[dict[str, Any]] = []
    for employee in await list_employees(session, country=Country.UAE):
        if employee.joined_on > as_of:
            continue
        end = min(as_of, employee.left_on) if employee.left_on else as_of
        salary = salary_on(employee, end)
        basic = rules.ZERO
        if salary:
            basic = sum(
                (dec(c["amount"]) for c in salary.components if c["code"] == "BASIC"), rules.ZERO
            )
        rows.append(
            {
                "employee_id": employee.id,
                "code": employee.code,
                "full_name": employee.full_name,
                "joined_on": employee.joined_on,
                "left_on": employee.left_on,
                "basic": rules.r2(basic),
                "service_years": rules.service_years(employee.joined_on, end),
                "gratuity": rules.uae_gratuity(basic, employee.joined_on, end),
            }
        )
    return rows


# ── runs ──────────────────────────────────────────────────────────────


def period_of(run: PayrollRun) -> Period:
    return Period(run.year, run.month)


async def list_runs(
    session: AsyncSession, *, country: str | None = None, year: int | None = None
) -> list[PayrollRun]:
    query = select(PayrollRun).order_by(
        PayrollRun.year.desc(), PayrollRun.month.desc(), PayrollRun.country
    )
    if country:
        query = query.where(PayrollRun.country == country)
    if year:
        query = query.where(PayrollRun.year == year)
    return list((await session.scalars(query)).unique().all())


async def get_run(session: AsyncSession, run_id: uuid.UUID) -> PayrollRun:
    run = await session.get(PayrollRun, run_id)
    if run is None:
        raise PayrollNotFound("No such payroll month.")
    return run


def _require(run: PayrollRun, *allowed: RunStatus, doing: str) -> None:
    if run.status not in allowed:
        raise PayrollConflict(
            f"This month is {run.status}, so it cannot be {doing}. "
            + {
                RunStatus.SUBMITTED: "It is waiting for approval; an approver can return it.",
                RunStatus.APPROVED: "It is approved; only a super admin can reopen it.",
                RunStatus.PAID: "It has been paid; only a super admin can reopen it.",
                RunStatus.DRAFT: "It has not been submitted yet.",
            }[RunStatus(run.status)]
        )


async def create_run(
    session: AsyncSession, *, country: str, year: int, month: int, note: str | None, actor: User
) -> PayrollRun:
    try:
        period = Period(year, month)
    except RuleError as exc:
        raise PayrollError(str(exc)) from exc
    existing = await session.scalar(
        select(PayrollRun).where(
            PayrollRun.country == country, PayrollRun.year == year, PayrollRun.month == month
        )
    )
    if existing is not None:
        raise PayrollConflict(
            f"{_country(country)} payroll for {period.label} already exists ({existing.status})."
        )
    run = PayrollRun(
        country=Country(country),
        year=year,
        month=month,
        currency=CURRENCY[country],
        status=RunStatus.DRAFT,
        note=note,
        created_by_id=actor.id,
        created_by=actor,
        lines=[],
        warnings=[],
    )
    session.add(run)
    await session.flush()
    await _calculate(session, run)
    await log(
        session, actor, "run.created",
        f"Started {_country(country)} payroll for {period.label}: {run.headcount} people",
        run_id=run.id,
    )
    await session.flush()
    return run


async def recalculate(session: AsyncSession, run: PayrollRun, actor: User) -> PayrollRun:
    _require(run, RunStatus.DRAFT, doing="recalculated")
    before = (run.headcount, run.net)
    await _calculate(session, run)
    await log(
        session, actor, "run.recalculated",
        f"Recalculated {period_of(run).label}: {run.headcount} people, net "
        f"{run.currency} {run.net} (was {before[0]} people, {run.currency} {before[1]})",
        run_id=run.id,
    )
    await session.flush()
    return run


async def update_line(
    session: AsyncSession,
    run: PayrollRun,
    line_id: uuid.UUID,
    changes: dict[str, Any],
    actor: User,
) -> PayrollLine:
    _require(run, RunStatus.DRAFT, doing="edited")
    line = next((ln for ln in run.lines if ln.id == line_id), None)
    if line is None:
        raise PayrollNotFound("No such line in this month.")
    if "extras" in changes:
        try:
            changes["extras"] = rules.clean_extras(changes["extras"])
        except RuleError as exc:
            raise PayrollError(str(exc)) from exc
    for key in ("lop_adjust", "ot_normal_hours", "ot_premium_hours", "tds_override"):
        if changes.get(key) is not None:
            changes[key] = rules.r2(changes[key])
    changed: dict[str, Any] = {}
    for key, value in changes.items():
        if value is None and key not in {"tds_override", "note"}:
            continue
        old = getattr(line, key)
        if old != value:
            changed[key] = {"from": old, "to": value}
            setattr(line, key, value)
    if not changed:
        return line
    await _calculate(session, run, only=line)
    await log(
        session, actor, "line.updated",
        f"Changed {line.employee_name}'s {period_of(run).label} inputs: "
        + ", ".join(sorted(changed)),
        run_id=run.id, employee_id=line.employee_id, detail=changed,
    )
    await session.flush()
    return line


async def delete_run(session: AsyncSession, run: PayrollRun, actor: User) -> None:
    _require(run, RunStatus.DRAFT, doing="deleted")
    label = f"{_country(run.country)} payroll for {period_of(run).label}"
    await log(session, actor, "run.deleted", f"Deleted the draft {label}", run_id=None,
              detail={"run": str(run.id), "headcount": run.headcount, "net": run.net})
    await session.delete(run)
    await session.flush()


async def submit(session: AsyncSession, run: PayrollRun, actor: User) -> PayrollRun:
    _require(run, RunStatus.DRAFT, doing="submitted")
    # Recalculated first, so what is approved is what the data says today —
    # not what it said when somebody last pressed the button.
    await _calculate(session, run)
    if not run.lines:
        raise PayrollError("There is nobody in this month to pay.")
    run.status = RunStatus.SUBMITTED
    run.submitted_by_id = actor.id
    run.submitted_by = actor
    run.submitted_at = datetime.now(UTC)
    run.approved_by_id = None
    run.approved_by = None
    run.approved_at = None
    await log(
        session, actor, "run.submitted",
        f"Submitted {_country(run.country)} payroll for {period_of(run).label} for approval: "
        f"{run.headcount} people, net {run.currency} {run.net}",
        run_id=run.id,
    )
    await session.flush()
    settings = await get_settings(session)
    if settings.notify_in_app:
        approvers = [u for u in await payroll_access.approver_users(session) if u.id != actor.id]
        await _notify(
            session, approvers, run,
            f"Payroll to approve: {_country(run.country)} {period_of(run).label}",
            f"{actor.display_name} submitted {run.headcount} payslips, net {run.currency} {run.net}.",
        )
    return run


async def approve(
    session: AsyncSession, run: PayrollRun, actor: User, *, own_allowed: bool = False
) -> PayrollRun:
    """``own_allowed``: a super admin may approve their own submission."""
    _require(run, RunStatus.SUBMITTED, doing="approved")
    if run.submitted_by_id == actor.id and not own_allowed:
        raise PayrollConflict(
            "You submitted this month, so somebody else has to approve it — a super admin."
        )
    run.status = RunStatus.APPROVED
    run.approved_by_id = actor.id
    run.approved_by = actor
    run.approved_at = datetime.now(UTC)
    run.returned_note = None
    await log(
        session, actor, "run.approved",
        f"Approved {_country(run.country)} payroll for {period_of(run).label}: "
        f"net {run.currency} {run.net}",
        run_id=run.id,
    )
    await session.flush()
    await _tell_submitter(session, run, f"Payroll approved: {period_of(run).label}",
                          f"{actor.display_name} approved it. Bank files can now be downloaded.")
    return run


async def return_run(session: AsyncSession, run: PayrollRun, note: str, actor: User) -> PayrollRun:
    _require(run, RunStatus.SUBMITTED, doing="returned")
    run.status = RunStatus.DRAFT
    run.returned_note = note.strip()
    await log(
        session, actor, "run.returned",
        f"Returned {_country(run.country)} payroll for {period_of(run).label}: {note.strip()}",
        run_id=run.id, detail={"note": note.strip()},
    )
    await session.flush()
    await _tell_submitter(session, run, f"Payroll returned: {period_of(run).label}",
                          f"{actor.display_name}: {note.strip()}")
    return run


async def mark_paid(session: AsyncSession, run: PayrollRun, paid_on: date, actor: User) -> PayrollRun:
    _require(run, RunStatus.APPROVED, doing="marked paid")
    run.status = RunStatus.PAID
    run.paid_on = paid_on
    run.paid_by_id = actor.id
    run.paid_by = actor
    await log(
        session, actor, "run.paid",
        f"Marked {_country(run.country)} payroll for {period_of(run).label} paid on "
        f"{paid_on.isoformat()}",
        run_id=run.id,
    )
    await session.flush()
    return run


async def reopen(session: AsyncSession, run: PayrollRun, reason: str, actor: User) -> PayrollRun:
    """Back to draft from approved or paid. Super admins only — the router says so."""
    _require(run, RunStatus.APPROVED, RunStatus.PAID, doing="reopened")
    was = run.status
    run.status = RunStatus.DRAFT
    run.approved_by_id = None
    run.approved_by = None
    run.approved_at = None
    run.paid_on = None
    run.paid_by_id = None
    run.paid_by = None
    run.returned_note = f"Reopened by {actor.display_name}: {reason.strip()}"
    await log(
        session, actor, "run.reopened",
        f"Reopened the {was} {_country(run.country)} payroll for {period_of(run).label}: "
        f"{reason.strip()}",
        run_id=run.id, detail={"reason": reason.strip(), "was": was},
    )
    await session.flush()
    return run


async def _notify(
    session: AsyncSession, users: list[User], run: PayrollRun, title: str, body: str
) -> None:
    if not users:
        return
    from app.notifications import service as notifications

    await notifications.notify(
        session,
        users=users,
        kind=NotificationKind.GENERAL,
        title=title,
        body=body,
        link=f"/payroll/runs/{run.id}",
        source="payroll",
    )


async def _tell_submitter(session: AsyncSession, run: PayrollRun, title: str, body: str) -> None:
    settings = await get_settings(session)
    if not settings.notify_in_app or run.submitted_by is None:
        return
    await _notify(session, [run.submitted_by], run, title, body)


# ── the calculation ───────────────────────────────────────────────────


async def _unpaid_leave_days(
    session: AsyncSession, user_ids: Iterable[uuid.UUID], period: Period
) -> dict[uuid.UUID, Decimal]:
    """Approved unpaid leave falling inside the month, by login."""
    ids = [u for u in user_ids if u]
    if not ids:
        return {}
    rows = (
        await session.scalars(
            select(LeaveRequest).where(
                LeaveRequest.user_id.in_(ids),
                LeaveRequest.leave_type == LeaveType.UNPAID,
                LeaveRequest.status == LeaveStatus.APPROVED,
                LeaveRequest.start_date <= period.end,
                LeaveRequest.end_date >= period.start,
            )
        )
    ).unique().all()
    days: dict[uuid.UUID, Decimal] = defaultdict(lambda: Decimal("0"))
    for request in rows:
        start = max(request.start_date, period.start)
        end = min(request.end_date, period.end)
        days[request.user_id] += Decimal((end - start).days + 1)
    return dict(days)


async def _tax_so_far(
    session: AsyncSession, employee_ids: Iterable[uuid.UUID], period: Period
) -> dict[uuid.UUID, TaxSoFar]:
    """Taxable salary and TDS in approved months earlier in this financial year."""
    ids = list(employee_ids)
    if not ids:
        return {}
    index = PayrollRun.year * 12 + PayrollRun.month
    rows = (
        await session.execute(
            select(PayrollLine.employee_id, func.sum(PayrollLine.taxable), func.sum(PayrollLine.tds))
            .join(PayrollRun, PayrollRun.id == PayrollLine.run_id)
            .where(
                PayrollLine.employee_id.in_(ids),
                PayrollRun.country == Country.INDIA,
                PayrollRun.status.in_([RunStatus.APPROVED, RunStatus.PAID]),
                and_(
                    index >= period.fy * 12 + 4,
                    index < period.year * 12 + period.month,
                ),
            )
            .group_by(PayrollLine.employee_id)
        )
    ).all()
    return {
        employee_id: TaxSoFar(taxable=dec(taxable), tds=dec(tds))
        for employee_id, taxable, tds in rows
    }


async def _earlier_unapproved(session: AsyncSession, period: Period) -> list[str]:
    index = PayrollRun.year * 12 + PayrollRun.month
    rows = (
        await session.scalars(
            select(PayrollRun).where(
                PayrollRun.country == Country.INDIA,
                PayrollRun.status.in_([RunStatus.DRAFT, RunStatus.SUBMITTED]),
                index >= period.fy * 12 + 4,
                index < period.year * 12 + period.month,
            )
        )
    ).unique().all()
    return [period_of(r).label for r in rows]


def _identifiers(employee: PayrollEmployee) -> dict[str, Any]:
    common = {
        "email": employee.email,
        "department": employee.department,
        "joined_on": employee.joined_on.isoformat(),
        "left_on": employee.left_on.isoformat() if employee.left_on else None,
        "bank_name": employee.bank_name,
        "bank_account": employee.bank_account,
    }
    if employee.country == Country.UAE:
        return {
            **common,
            "iban": employee.iban,
            "bank_routing": employee.bank_routing,
            "mol_person_id": employee.mol_person_id,
        }
    return {
        **common,
        "ifsc": employee.ifsc,
        "pan": employee.pan,
        "uan": employee.uan,
        "esi_number": employee.esi_number,
        "pf_enabled": employee.pf_enabled,
    }


def _facts(employee: PayrollEmployee, salary: PayrollSalary) -> EmployeeFacts:
    return EmployeeFacts(
        country=employee.country,
        joined_on=employee.joined_on,
        left_on=employee.left_on,
        components=tuple(salary.components),
        pf_enabled=employee.pf_enabled,
        pf_on_actual=employee.pf_on_actual,
        eps_member=employee.eps_member,
        esi_mode=employee.esi_mode,
        pt_state=employee.pt_state,
        pt_override=employee.pt_override,
        tds_monthly_override=employee.tds_monthly_override,
    )


def _totals(run: PayrollRun) -> None:
    lines = list(run.lines)
    run.headcount = len(lines)
    run.gross = rules.r2(sum((dec(ln.gross) for ln in lines), Decimal("0")))
    run.deductions = rules.r2(sum((dec(ln.total_deductions) for ln in lines), Decimal("0")))
    run.net = rules.r2(sum((dec(ln.net) for ln in lines), Decimal("0")))
    run.employer_cost = rules.r2(sum((dec(ln.employer_cost) for ln in lines), Decimal("0")))


async def _calculate(
    session: AsyncSession, run: PayrollRun, *, only: PayrollLine | None = None
) -> None:
    """Bring the run's lines in line with the employees, then work out each one.

    With ``only``, just that line is recalculated and the run's totals redone —
    the case of a processor typing overtime into one row of forty.
    """
    period = period_of(run)
    settings = await get_settings(session)
    uae_rules, india_rules = rules_from(settings)
    table: TaxTable | None = None
    run_warnings: list[str] = []
    if run.country == Country.INDIA:
        table = rules.tax_table_for(period.fy, settings.in_tax_tables)
        earlier = await _earlier_unapproved(session, period)
        if earlier:
            run_warnings.append(
                "Earlier months of this financial year are not approved yet ("
                + ", ".join(earlier)
                + "): their tax is not counted in this month's TDS."
            )

    lines_by_employee = {line.employee_id: line for line in run.lines}

    if only is None:
        employees = (
            await session.scalars(
                select(PayrollEmployee).where(
                    PayrollEmployee.country == run.country,
                    PayrollEmployee.joined_on <= period.end,
                    or_(PayrollEmployee.left_on.is_(None), PayrollEmployee.left_on >= period.start),
                )
            )
        ).all()
        payable: dict[uuid.UUID, tuple[PayrollEmployee, PayrollSalary]] = {}
        for employee in employees:
            salary = salary_on(employee, min(period.end, employee.left_on or period.end))
            if salary is None:
                run_warnings.append(
                    f"{employee.full_name} ({employee.code}) has no salary in force for "
                    f"{period.label} and is left out."
                )
                continue
            payable[employee.id] = (employee, salary)
        for employee_id, line in list(lines_by_employee.items()):
            if employee_id not in payable:
                run.lines.remove(line)
                del lines_by_employee[employee_id]
        for employee_id, (employee, _salary) in payable.items():
            if employee_id not in lines_by_employee:
                line = PayrollLine(
                    run_id=run.id,
                    employee_id=employee.id,
                    employee_code=employee.code,
                    employee_name=employee.full_name,
                    extras=[],
                    lop_adjust=Decimal("0"),
                    ot_normal_hours=Decimal("0"),
                    ot_premium_hours=Decimal("0"),
                    on_hold=False,
                )
                run.lines.append(line)
                lines_by_employee[employee_id] = line
        targets = [(lines_by_employee[eid], emp, sal) for eid, (emp, sal) in payable.items()]
    else:
        employee = await get_employee(session, only.employee_id)
        salary = salary_on(employee, min(period.end, employee.left_on or period.end))
        if salary is None:
            raise PayrollError(
                f"{employee.full_name} has no salary in force for {period.label}. "
                f"Recalculate the month to drop them."
            )
        targets = [(only, employee, salary)]
        run_warnings = [w for w in (run.warnings or []) if "is left out" in w] + run_warnings

    leave = await _unpaid_leave_days(session, [emp.user_id for _, emp, _ in targets], period)
    so_far = (
        await _tax_so_far(session, [emp.id for _, emp, _ in targets], period)
        if run.country == Country.INDIA
        else {}
    )

    for line, employee, salary in targets:
        line.employee_code = employee.code
        line.employee_name = employee.full_name
        line.designation = employee.designation
        line.identifiers = _identifiers(employee)
        line.leave_lop_days = rules.r2(leave.get(employee.user_id, 0) if employee.user_id else 0)

        prior = so_far.get(employee.id, TaxSoFar())
        if employee.opening_fy == period.fy:
            prior = TaxSoFar(
                taxable=prior.taxable + dec(employee.opening_taxable),
                tds=prior.tds + dec(employee.opening_tds),
            )
        inputs = Inputs(
            leave_lop_days=dec(line.leave_lop_days),
            lop_adjust=dec(line.lop_adjust),
            ot_normal_hours=dec(line.ot_normal_hours),
            ot_premium_hours=dec(line.ot_premium_hours),
            extras=rules.extras_from_json(line.extras),
            tds_override=line.tds_override,
        )
        slip = rules.calculate(
            period,
            _facts(employee, salary),
            inputs,
            uae=uae_rules,
            india=india_rules,
            tax=table,
            so_far=prior,
        )

        warnings = list(slip.warnings)
        if salary.effective_from > period.start and salary.effective_from <= period.end:
            warnings.append(
                f"Salary changed on {salary.effective_from.isoformat()}; the new structure is "
                f"used for the whole month. Add the difference for the earlier days as an extra "
                f"if it matters."
            )
        if run.country == Country.INDIA:
            fy_start = date(period.fy, 4, 1)
            worked_earlier = max(employee.joined_on, fy_start) < period.start
            if worked_earlier and prior.taxable == 0 and employee.opening_fy != period.fy:
                warnings.append(
                    f"No earlier salary is recorded for FY {fy_label(period.fy)} — no approved "
                    f"months and no opening figures — so TDS projects from this month only. "
                    f"Enter the opening figures on the employee."
                )
            if not employee.pan and slip.tds > 0:
                warnings.append("No PAN on file: TDS without a PAN is at least 20 % by law.")
        if line.on_hold:
            warnings.append("On hold: left out of the bank file.")

        # Rounded as the columns are, so a fresh calculation answers "28.00"
        # exactly as a reload from the database does.
        line.basis_days = rules.r2(slip.basis_days)
        line.paid_days = rules.r2(slip.paid_days)
        line.unpaid_days = rules.r2(slip.unpaid_days)
        line.earnings = rules.to_json(slip.earnings)
        line.deductions = rules.to_json(slip.deductions)
        line.employer = rules.to_json(slip.employer)
        line.gross = slip.gross
        line.total_deductions = slip.total_deductions
        line.net = slip.net
        line.employer_cost = slip.employer_cost
        line.taxable = slip.taxable
        line.tds = slip.tds
        line.workings = rules.to_json(slip.workings)
        line.warnings = warnings

    if table is not None and run.country == Country.INDIA:
        if table.fy != period.fy:
            run_warnings.append(
                f"No FY {fy_label(period.fy)} income tax table is saved; TDS uses "
                f"FY {fy_label(table.fy)}'s. Save this year's in payroll settings."
            )
        elif not table.confirmed:
            run_warnings.append(
                f"The FY {fy_label(table.fy)} income tax table has not been confirmed."
            )
    elif run.country == Country.INDIA:
        run_warnings.append("No income tax table is saved: TDS is 0 for everyone.")

    _totals(run)
    run.warnings = list(dict.fromkeys(run_warnings))
    run.calculated_at = datetime.now(UTC)
    await session.flush()
