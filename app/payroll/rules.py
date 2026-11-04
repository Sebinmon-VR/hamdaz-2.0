"""How a month's pay is worked out. No database, no network — figures in, payslip out.

Everything that decides a number lives here, and only here, so that the whole
calculation can be read in one file and tested on paper. The service gathers
the facts (the salary structure in force, approved unpaid leave, what was paid
earlier in the financial year) and hands them in; this decides nothing about
who may do what.

**Days.** A month's pay is earned per day at ``monthly / basis``, where the
basis is 30 (UAE practice) or the days in that month (common in India) — a
setting per country. Somebody employed for the whole month is paid ``basis −
unpaid days``; somebody who joined or left during it is paid for the days they
were employed, less unpaid days. Every earning is prorated by the same
fraction, so PF, ESI and tax are worked on what was actually earned.

**UAE** (Federal Decree-Law 33 of 2021): no income tax. Overtime is paid on
the basic wage — ordinary hours at 1.25×, night, rest-day and holiday hours at
1.5×. Deductions over half the wage are flagged, because the law caps them
there. End-of-service gratuity is 21 days' basic for each of the first five
years and 30 days' for each year after, nothing before a year's service, and
never more than two years' basic; a day's basic is ``basic / 30``. The month's
gratuity *provision* is the change in that liability over the month.

**India**: PF at 12 % each side on basic + DA up to the ₹15,000 ceiling (or on
actual basic where the employee opts in), the employer's share split 8.33 % to
pension on wages up to the ceiling and the rest to PF; EDLI and admin charges
as employer cost. ESI at 0.75 % / 3.25 % of gross, rounded up, for people whose
monthly pay is within the ₹21,000 threshold. Professional tax from the state's
slab table. TDS under the new regime, projected across the financial year:
what was paid so far plus this month plus the regular salary for the months
left, less the standard deduction, taxed by slab with the 87A rebate and its
marginal relief, surcharge with marginal relief, and 4 % cess — then what is
still owed is spread over this month and the months left.

Every rate and slab above is a setting, not a constant, and the defaults are
the published figures as of FY 2025-26. Tax tables carry a ``confirmed`` flag
because they change by Finance Act and the processor, not this file, is the
one who knows when they have.
"""

from __future__ import annotations

import calendar
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from typing import Any, Final, Iterable

ZERO: Final = Decimal("0")
ONE: Final = Decimal("1")
CENT: Final = Decimal("0.01")
HUNDRED: Final = Decimal("100")


def dec(value: Any) -> Decimal:
    """A Decimal from whatever arrived: a column, a JSON string, a number."""
    if value is None or value == "":
        return ZERO
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def r2(value: Any) -> Decimal:
    return dec(value).quantize(CENT, ROUND_HALF_UP)


def r0(value: Any) -> Decimal:
    """Nearest whole unit — how EPFO and the tax department round."""
    return dec(value).quantize(ONE, ROUND_HALF_UP)


def ceil0(value: Any) -> Decimal:
    """Up to the next whole rupee — how ESIC rounds a contribution."""
    return dec(value).quantize(ONE, ROUND_CEILING)


def pct(rate: Any) -> Decimal:
    return dec(rate) / HUNDRED


class RuleError(ValueError):
    """Input that cannot be calculated — said in words a processor can act on."""


# ── the salary components ─────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ComponentSpec:
    code: str
    name: str
    countries: frozenset[str]
    #: The basic wage: the base for UAE overtime and gratuity.
    basic: bool = False
    #: India: counts as wages for PF (basic and dearness allowance).
    pf_wage: bool = False
    #: May appear more than once, each under its own name.
    repeatable: bool = False


COMPONENTS: Final[tuple[ComponentSpec, ...]] = (
    ComponentSpec("BASIC", "Basic salary", frozenset({"AE", "IN"}), basic=True, pf_wage=True),
    ComponentSpec("HOUSING", "Housing allowance", frozenset({"AE"})),
    ComponentSpec("TRANSPORT", "Transport allowance", frozenset({"AE"})),
    ComponentSpec("DA", "Dearness allowance", frozenset({"IN"}), pf_wage=True),
    ComponentSpec("HRA", "House rent allowance", frozenset({"IN"})),
    ComponentSpec("SPECIAL", "Special allowance", frozenset({"IN"})),
    ComponentSpec("OTHER", "Other allowance", frozenset({"AE", "IN"}), repeatable=True),
)

COMPONENT: Final[dict[str, ComponentSpec]] = {c.code: c for c in COMPONENTS}


def clean_components(country: str, raw: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    """Check a salary structure and return it in stored form.

    A basic salary is required: without one there is no base for overtime,
    gratuity or PF, and every one of those would silently come out as zero.
    """
    cleaned: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        code = str(item.get("code") or "").strip().upper()
        spec = COMPONENT.get(code)
        if spec is None:
            raise RuleError(f"Unknown salary component {code!r}.")
        if country not in spec.countries:
            raise RuleError(f"{spec.name} is not a {_country_name(country)} component.")
        if code in seen and not spec.repeatable:
            raise RuleError(f"{spec.name} appears twice.")
        seen.add(code)
        try:
            amount = r2(item.get("amount"))
        except Exception as exc:  # noqa: BLE001 - any unparsable value
            raise RuleError(f"{spec.name}: the amount is not a number.") from exc
        if amount < 0:
            raise RuleError(f"{spec.name}: the amount cannot be negative.")
        name = str(item.get("name") or "").strip() or spec.name
        cleaned.append({"code": code, "name": name[:80], "amount": str(amount)})
    basic = [c for c in cleaned if c["code"] == "BASIC"]
    if not basic or dec(basic[0]["amount"]) <= 0:
        raise RuleError("A salary structure needs a basic salary above zero.")
    return cleaned


def _country_name(country: str) -> str:
    return {"AE": "UAE", "IN": "India"}.get(country, country)


# ── the period ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Period:
    year: int
    month: int

    def __post_init__(self) -> None:
        if not 1 <= self.month <= 12:
            raise RuleError("Month must be 1 to 12.")
        if not 2000 <= self.year <= 2100:
            raise RuleError("That year is out of range.")

    @property
    def days(self) -> int:
        return calendar.monthrange(self.year, self.month)[1]

    @property
    def start(self) -> date:
        return date(self.year, self.month, 1)

    @property
    def end(self) -> date:
        return date(self.year, self.month, self.days)

    @property
    def label(self) -> str:
        return f"{calendar.month_name[self.month]} {self.year}"

    @property
    def previous(self) -> Period:
        return Period(self.year - 1, 12) if self.month == 1 else Period(self.year, self.month - 1)

    # India's financial year runs April to March.

    @property
    def fy(self) -> int:
        """The financial year's starting year: November 2026 is FY 2026 (2026-27)."""
        return self.year if self.month >= 4 else self.year - 1

    @property
    def fy_end(self) -> date:
        return date(self.fy + 1, 3, 31)

    def months_left_in_fy(self) -> int:
        """Months after this one, up to and including March."""
        return (12 - self.month) + 3 if self.month >= 4 else 3 - self.month


def fy_label(fy: int) -> str:
    return f"{fy}-{str(fy + 1)[-2:]}"


# ── settings, as the rules see them ───────────────────────────────────


@dataclass(frozen=True, slots=True)
class UaeRules:
    day_basis: str = "fixed_30"
    hours_per_day: Decimal = Decimal("8")
    ot_normal_rate: Decimal = Decimal("1.25")
    ot_premium_rate: Decimal = Decimal("1.50")


@dataclass(frozen=True, slots=True)
class PtSlab:
    min_gross: Decimal
    amount: Decimal
    #: Some states (Maharashtra) collect a different amount in February.
    feb_amount: Decimal | None = None


def pt_slabs_from_json(raw: dict[str, Any] | None) -> dict[str, tuple[PtSlab, ...]]:
    slabs: dict[str, tuple[PtSlab, ...]] = {}
    for state, rows in (raw or {}).items():
        parsed = [
            PtSlab(
                min_gross=r2(row.get("min_gross")),
                amount=r2(row.get("amount")),
                feb_amount=(
                    None
                    if row.get("feb_amount") in (None, "")
                    else r2(row.get("feb_amount"))
                ),
            )
            for row in rows or []
        ]
        slabs[str(state).strip().upper()] = tuple(sorted(parsed, key=lambda s: s.min_gross))
    return slabs


def clean_pt_slabs(raw: dict[str, Any] | None) -> dict[str, list[dict[str, str | None]]]:
    """Validate and normalise the professional tax table for storage."""
    out: dict[str, list[dict[str, str | None]]] = {}
    for state, rows in (raw or {}).items():
        code = str(state).strip().upper()
        if not code.isalpha() or not 2 <= len(code) <= 4:
            raise RuleError(f"{state!r} is not a state code. Use two letters: KA, MH, KL.")
        try:
            slabs = pt_slabs_from_json({code: rows})[code]
        except Exception as exc:  # noqa: BLE001
            raise RuleError(f"Professional tax for {code}: every figure must be a number.") from exc
        if any(s.min_gross < 0 or s.amount < 0 for s in slabs):
            raise RuleError(f"Professional tax for {code}: figures cannot be negative.")
        out[code] = [
            {
                "min_gross": str(s.min_gross),
                "amount": str(s.amount),
                "feb_amount": None if s.feb_amount is None else str(s.feb_amount),
            }
            for s in slabs
        ]
    return out


@dataclass(frozen=True, slots=True)
class IndiaRules:
    day_basis: str = "calendar"
    pf_ee_rate: Decimal = Decimal("12")
    pf_er_rate: Decimal = Decimal("12")
    eps_rate: Decimal = Decimal("8.33")
    pf_ceiling: Decimal = Decimal("15000")
    edli_rate: Decimal = Decimal("0.50")
    pf_admin_rate: Decimal = Decimal("0.50")
    wage_code_rule: bool = False
    esi_threshold: Decimal = Decimal("21000")
    esi_ee_rate: Decimal = Decimal("0.75")
    esi_er_rate: Decimal = Decimal("3.25")
    pt_slabs: dict[str, tuple[PtSlab, ...]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TaxTable:
    """One financial year's new-regime income tax, as the Finance Act set it."""

    fy: int
    #: ``(upper bound, rate %)``, ascending; the last bound is None.
    slabs: tuple[tuple[Decimal | None, Decimal], ...]
    standard_deduction: Decimal
    #: Section 87A: taxable income up to this pays no tax...
    rebate_limit: Decimal
    #: ...up to this much rebate.
    rebate_max: Decimal
    cess_rate: Decimal
    #: ``(income above, rate %)``, ascending.
    surcharge: tuple[tuple[Decimal, Decimal], ...]
    confirmed: bool = False
    confirmed_by: str | None = None
    confirmed_at: str | None = None
    source: str | None = None

    @classmethod
    def from_json(cls, fy: int, raw: dict[str, Any]) -> TaxTable:
        slabs = tuple(
            (None if row.get("upto") in (None, "") else r2(row["upto"]), r2(row["rate"]))
            for row in raw.get("slabs", [])
        )
        if not slabs or slabs[-1][0] is not None:
            raise RuleError("A tax table's last slab must have no upper limit.")
        bounds = [b for b, _ in slabs if b is not None]
        if bounds != sorted(bounds) or len(set(bounds)) != len(bounds):
            raise RuleError("Tax slabs must be in ascending order.")
        return cls(
            fy=fy,
            slabs=slabs,
            standard_deduction=r2(raw.get("standard_deduction")),
            rebate_limit=r2(raw.get("rebate_limit")),
            rebate_max=r2(raw.get("rebate_max")),
            cess_rate=r2(raw.get("cess_rate", "4")),
            surcharge=tuple(
                (r2(row["above"]), r2(row["rate"])) for row in raw.get("surcharge", [])
            ),
            confirmed=bool(raw.get("confirmed")),
            confirmed_by=raw.get("confirmed_by"),
            confirmed_at=raw.get("confirmed_at"),
            source=raw.get("source"),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "slabs": [
                {"upto": None if b is None else str(b), "rate": str(r)} for b, r in self.slabs
            ],
            "standard_deduction": str(self.standard_deduction),
            "rebate_limit": str(self.rebate_limit),
            "rebate_max": str(self.rebate_max),
            "cess_rate": str(self.cess_rate),
            "surcharge": [{"above": str(a), "rate": str(r)} for a, r in self.surcharge],
            "confirmed": self.confirmed,
            "confirmed_by": self.confirmed_by,
            "confirmed_at": self.confirmed_at,
            "source": self.source,
        }

    def slab_tax(self, income: Decimal) -> Decimal:
        tax = ZERO
        lower = ZERO
        for upper, rate in self.slabs:
            if upper is None or income <= upper:
                if income > lower:
                    tax += (income - lower) * pct(rate)
                break
            tax += (upper - lower) * pct(rate)
            lower = upper
        return tax

    def annual_tax(self, income: Decimal) -> dict[str, Decimal]:
        """Tax on a year's taxable income, every step kept for the payslip workings."""
        income = max(ZERO, income)
        base = self.slab_tax(income)

        rebate = ZERO
        if income <= self.rebate_limit:
            rebate = min(base, self.rebate_max)
        elif base > income - self.rebate_limit:
            # Marginal relief: just over the limit, tax cannot exceed the
            # amount by which income exceeds it.
            rebate = base - (income - self.rebate_limit)
        after_rebate = base - rebate

        surcharge = ZERO
        tier_index = None
        for i, (above, _rate) in enumerate(self.surcharge):
            if income > above:
                tier_index = i
        if tier_index is not None:
            above, rate = self.surcharge[tier_index]
            full = after_rebate * pct(rate)
            previous_rate = self.surcharge[tier_index - 1][1] if tier_index > 0 else ZERO
            # Marginal relief: tax and surcharge together may not grow by more
            # than income grew past the threshold.
            at_threshold = self.slab_tax(above) * (ONE + pct(previous_rate))
            surcharge = max(ZERO, min(full, at_threshold + (income - above) - after_rebate))

        cess = (after_rebate + surcharge) * pct(self.cess_rate)
        return {
            "slab_tax": r2(base),
            "rebate": r2(rebate),
            "surcharge": r2(surcharge),
            "cess": r2(cess),
            "total": r0(after_rebate + surcharge + cess),
        }


#: FY 2025-26, as the Finance Act 2025 set the new regime. Seeded unconfirmed:
#: a processor checks it, and saves the next year's table when it changes.
DEFAULT_TAX_TABLES: Final[dict[str, dict[str, Any]]] = {
    "2025": {
        "slabs": [
            {"upto": "400000", "rate": "0"},
            {"upto": "800000", "rate": "5"},
            {"upto": "1200000", "rate": "10"},
            {"upto": "1600000", "rate": "15"},
            {"upto": "2000000", "rate": "20"},
            {"upto": "2400000", "rate": "25"},
            {"upto": None, "rate": "30"},
        ],
        "standard_deduction": "75000",
        "rebate_limit": "1200000",
        "rebate_max": "60000",
        "cess_rate": "4",
        "surcharge": [
            {"above": "5000000", "rate": "10"},
            {"above": "10000000", "rate": "15"},
            {"above": "20000000", "rate": "25"},
        ],
        "confirmed": False,
        "confirmed_by": None,
        "confirmed_at": None,
        "source": "Finance Act 2025, new regime (section 115BAC)",
    }
}


def tax_table_for(fy: int, raw_tables: dict[str, Any] | None) -> TaxTable | None:
    """The table for ``fy``, else the latest earlier one. None if there is none."""
    tables = raw_tables or {}
    years = sorted((int(k) for k in tables if str(k).isdigit()), reverse=True)
    for year in years:
        if year <= fy:
            return TaxTable.from_json(year, tables[str(year)])
    return None


# ── one employee's month ──────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class EmployeeFacts:
    country: str
    joined_on: date
    left_on: date | None
    #: Stored form from ``clean_components``.
    components: tuple[dict[str, Any], ...]
    pf_enabled: bool = True
    pf_on_actual: bool = False
    eps_member: bool = True
    esi_mode: str = "auto"
    pt_state: str | None = None
    pt_override: Decimal | None = None
    tds_monthly_override: Decimal | None = None


@dataclass(frozen=True, slots=True)
class Extra:
    name: str
    amount: Decimal
    kind: str  # "earning" | "deduction"
    #: UAE: paid as variable pay in the WPS file (bonus, commission).
    variable: bool = True
    #: India: part of taxable salary.
    taxable: bool = True


def clean_extras(raw: Iterable[dict[str, Any]] | None) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for item in raw or []:
        name = str(item.get("name") or "").strip()
        if not name:
            raise RuleError("Every extra line needs a name.")
        kind = str(item.get("kind") or "earning")
        if kind not in ("earning", "deduction"):
            raise RuleError(f"{name}: kind must be earning or deduction.")
        try:
            amount = r2(item.get("amount"))
        except Exception as exc:  # noqa: BLE001
            raise RuleError(f"{name}: the amount is not a number.") from exc
        if amount <= 0:
            raise RuleError(f"{name}: the amount must be above zero.")
        cleaned.append(
            {
                "name": name[:80],
                "amount": str(amount),
                "kind": kind,
                "variable": bool(item.get("variable", True)),
                "taxable": bool(item.get("taxable", True)),
            }
        )
    return cleaned


def extras_from_json(raw: Iterable[dict[str, Any]] | None) -> tuple[Extra, ...]:
    return tuple(
        Extra(
            name=str(e["name"]),
            amount=r2(e["amount"]),
            kind=str(e["kind"]),
            variable=bool(e.get("variable", True)),
            taxable=bool(e.get("taxable", True)),
        )
        for e in raw or []
    )


@dataclass(frozen=True, slots=True)
class Inputs:
    leave_lop_days: Decimal = ZERO
    lop_adjust: Decimal = ZERO
    ot_normal_hours: Decimal = ZERO
    ot_premium_hours: Decimal = ZERO
    extras: tuple[Extra, ...] = ()
    tds_override: Decimal | None = None


@dataclass(frozen=True, slots=True)
class TaxSoFar:
    """India: taxable salary and TDS earlier in the financial year."""

    taxable: Decimal = ZERO
    tds: Decimal = ZERO


@dataclass
class Payslip:
    currency: str
    employed_days: int
    basis_days: Decimal
    paid_days: Decimal
    unpaid_days: Decimal
    earnings: list[dict[str, Any]] = field(default_factory=list)
    deductions: list[dict[str, Any]] = field(default_factory=list)
    employer: list[dict[str, Any]] = field(default_factory=list)
    gross: Decimal = ZERO
    total_deductions: Decimal = ZERO
    net: Decimal = ZERO
    employer_cost: Decimal = ZERO
    taxable: Decimal = ZERO
    tds: Decimal = ZERO
    workings: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def finish(self) -> Payslip:
        self.gross = r2(sum((dec(e["amount"]) for e in self.earnings), ZERO))
        self.total_deductions = r2(sum((dec(e["amount"]) for e in self.deductions), ZERO))
        self.net = r2(self.gross - self.total_deductions)
        self.employer_cost = r2(
            self.gross + sum((dec(e["amount"]) for e in self.employer), ZERO)
        )
        if self.net < 0:
            self.warnings.append(
                "Deductions are more than this month's pay: net pay is negative."
            )
        return self


def to_json(value: Any) -> Any:
    """Something JSONB can store: decimals, dates and ids as strings, all the way down.

    The log records "from" and "to" for whatever changed, and a joining date or
    a linked login is as likely to change as a salary — ``json.dumps`` refuses
    all three.
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(k): to_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_json(v) for v in value]
    return value


def _attendance(
    period: Period, emp: EmployeeFacts, inputs: Inputs, day_basis: str
) -> tuple[int, Decimal, Decimal, Decimal, Decimal]:
    """(employed days, basis, paid days, unpaid days, LOP days)."""
    start = max(period.start, emp.joined_on)
    end = min(period.end, emp.left_on) if emp.left_on else period.end
    employed = 0 if end < start else (end - start).days + 1
    basis = Decimal(30) if day_basis == "fixed_30" else Decimal(period.days)
    whole_month = employed == period.days
    base = basis if whole_month else min(basis, Decimal(employed))
    lop = max(ZERO, dec(inputs.leave_lop_days) + dec(inputs.lop_adjust))
    paid = max(ZERO, base - lop)
    return employed, basis, paid, basis - paid, lop


def _earned(
    emp: EmployeeFacts, fraction: Decimal
) -> tuple[list[dict[str, Any]], dict[str, Decimal]]:
    """Each structure component prorated, and the full and earned totals by flag."""
    lines: list[dict[str, Any]] = []
    totals = {"full": ZERO, "earned": ZERO, "basic_full": ZERO, "pf_wage": ZERO}
    for comp in emp.components:
        spec = COMPONENT[comp["code"]]
        full = r2(comp["amount"])
        earned = r2(full * fraction)
        lines.append(
            {
                "code": comp["code"],
                "name": comp["name"],
                "full": full,
                "amount": earned,
                "variable": False,
                "taxable": True,
            }
        )
        totals["full"] += full
        totals["earned"] += earned
        if spec.basic:
            totals["basic_full"] += full
        if spec.pf_wage:
            totals["pf_wage"] += earned
    return lines, totals


def uae_gratuity(basic_monthly: Decimal, joined_on: date, as_of: date) -> Decimal:
    """End-of-service gratuity owed on ``as_of``, at this basic wage."""
    basic = dec(basic_monthly)
    if as_of < joined_on or basic <= 0:
        return ZERO
    days = (as_of - joined_on).days + 1
    if days < 365:
        return ZERO
    years = Decimal(days) / Decimal(365)
    daily = basic / Decimal(30)
    first = min(years, Decimal(5)) * Decimal(21) * daily
    later = max(ZERO, years - Decimal(5)) * Decimal(30) * daily
    return r2(min(first + later, basic * Decimal(24)))


def calculate(
    period: Period,
    emp: EmployeeFacts,
    inputs: Inputs,
    *,
    uae: UaeRules | None = None,
    india: IndiaRules | None = None,
    tax: TaxTable | None = None,
    so_far: TaxSoFar | None = None,
) -> Payslip:
    if emp.country == "AE":
        return _uae(period, emp, inputs, uae or UaeRules())
    if emp.country == "IN":
        return _india(period, emp, inputs, india or IndiaRules(), tax, so_far or TaxSoFar())
    raise RuleError(f"Unknown country {emp.country!r}.")


def _extras_into(slip: Payslip, extras: Iterable[Extra]) -> None:
    for extra in extras:
        if extra.kind == "earning":
            slip.earnings.append(
                {
                    "code": "EXTRA",
                    "name": extra.name,
                    "full": extra.amount,
                    "amount": extra.amount,
                    "variable": extra.variable,
                    "taxable": extra.taxable,
                }
            )
        else:
            slip.deductions.append({"code": "EXTRA", "name": extra.name, "amount": extra.amount})


def _header(
    period: Period, emp: EmployeeFacts, inputs: Inputs, day_basis: str, currency: str
) -> tuple[Payslip, Decimal]:
    employed, basis, paid, unpaid, lop = _attendance(period, emp, inputs, day_basis)
    slip = Payslip(
        currency=currency,
        employed_days=employed,
        basis_days=basis,
        paid_days=paid,
        unpaid_days=unpaid,
    )
    slip.workings["days"] = {
        "basis": basis,
        "employed": employed,
        "lop": lop,
        "paid": paid,
        "rule": "monthly / 30" if day_basis == "fixed_30" else f"monthly / {period.days}",
    }
    if employed == 0:
        slip.warnings.append("Not employed in this month: joined after it or left before it.")
    elif lop > (basis if employed == period.days else Decimal(employed)):
        slip.warnings.append("More unpaid days than days employed: nothing is paid.")
    return slip, (paid / basis if basis else ZERO)


def _uae(period: Period, emp: EmployeeFacts, inputs: Inputs, rules: UaeRules) -> Payslip:
    slip, fraction = _header(period, emp, inputs, rules.day_basis, "AED")
    earned, totals = _earned(emp, fraction)
    slip.earnings.extend(earned)

    basic = totals["basic_full"]
    hours = dec(rules.hours_per_day) or Decimal(8)
    hourly = basic / slip.basis_days / hours if slip.basis_days else ZERO
    slip.workings["overtime"] = {"hourly_basic": r2(hourly)}
    for code, name, qty, rate in (
        ("OT_NORMAL", "Overtime", inputs.ot_normal_hours, rules.ot_normal_rate),
        ("OT_PREMIUM", "Overtime (night / rest day / holiday)", inputs.ot_premium_hours,
         rules.ot_premium_rate),
    ):
        qty = dec(qty)
        if qty <= 0:
            continue
        amount = r2(qty * hourly * dec(rate))
        slip.earnings.append(
            {
                "code": code,
                "name": f"{name} — {qty.normalize():f} h × {dec(rate).normalize():f}",
                "full": amount,
                "amount": amount,
                "variable": True,
                "taxable": True,
            }
        )
    _extras_into(slip, inputs.extras)

    # Gratuity: the liability at the end of this month (or the last day worked)
    # less the liability at the end of last month, both at today's basic.
    as_of = min(period.end, emp.left_on) if emp.left_on else period.end
    owed_now = uae_gratuity(basic, emp.joined_on, as_of)
    owed_before = uae_gratuity(basic, emp.joined_on, period.previous.end)
    provision = max(ZERO, r2(owed_now - owed_before))
    slip.workings["gratuity"] = {"liability": owed_now, "provision": provision}
    if provision > 0:
        slip.employer.append(
            {"code": "GRATUITY", "name": "End-of-service gratuity provision", "amount": provision}
        )

    slip.finish()
    if slip.gross > 0 and slip.total_deductions > slip.gross / 2:
        slip.warnings.append(
            "Deductions are more than half of this month's pay. UAE law caps "
            "deductions at 50 % of the wage."
        )
    if emp.left_on and period.start <= emp.left_on <= period.end:
        slip.warnings.append(
            f"Leaves on {emp.left_on.isoformat()}: end-of-service gratuity owed is "
            f"AED {owed_now}. If this is the final settlement, add it (and any "
            f"leave encashment) as an extra."
        )
    return slip


def _india(
    period: Period,
    emp: EmployeeFacts,
    inputs: Inputs,
    rules: IndiaRules,
    tax: TaxTable | None,
    so_far: TaxSoFar,
) -> Payslip:
    slip, fraction = _header(period, emp, inputs, rules.day_basis, "INR")
    earned, totals = _earned(emp, fraction)
    slip.earnings.extend(earned)
    if dec(inputs.ot_normal_hours) > 0 or dec(inputs.ot_premium_hours) > 0:
        slip.warnings.append(
            "Overtime hours are only calculated for the UAE. For India, add the "
            "overtime as an extra earning."
        )
    _extras_into(slip, inputs.extras)

    gross = r2(sum((dec(e["amount"]) for e in slip.earnings), ZERO))
    structure_earned = totals["earned"]

    # ── PF ──
    pf_wages = totals["pf_wage"]
    if rules.wage_code_rule and structure_earned > 0:
        excluded = structure_earned - pf_wages
        if excluded > structure_earned / 2:
            pf_wages += excluded - structure_earned / 2
    pf_wages = r2(pf_wages)
    if emp.pf_enabled:
        capped = min(pf_wages, dec(rules.pf_ceiling))
        base = pf_wages if emp.pf_on_actual else capped
        ee = r0(base * pct(rules.pf_ee_rate))
        er_total = r0(base * pct(rules.pf_er_rate))
        eps_wages = capped if emp.eps_member else ZERO
        eps = min(r0(eps_wages * pct(rules.eps_rate)), er_total)
        edli = r0(capped * pct(rules.edli_rate))
        admin = r0(base * pct(rules.pf_admin_rate))
        if ee > 0:
            slip.deductions.append({"code": "PF_EE", "name": "Provident fund", "amount": ee})
        if er_total - eps > 0:
            slip.employer.append(
                {"code": "PF_ER", "name": "Employer provident fund", "amount": er_total - eps}
            )
        if eps > 0:
            slip.employer.append(
                {"code": "PF_EPS", "name": "Employer pension (EPS)", "amount": eps}
            )
        if edli > 0:
            slip.employer.append({"code": "PF_EDLI", "name": "EDLI", "amount": edli})
        if admin > 0:
            slip.employer.append({"code": "PF_ADMIN", "name": "PF admin charges", "amount": admin})
        slip.workings["pf"] = {
            "pf_wages": pf_wages,
            "epf_wages": base,
            "eps_wages": eps_wages,
            "edli_wages": capped,
            "ee": ee,
            "eps": eps,
            "er_epf": er_total - eps,
        }

    # ── ESI ──
    full_gross = totals["full"]
    covered = emp.esi_mode == "yes" or (
        emp.esi_mode == "auto" and 0 < full_gross <= dec(rules.esi_threshold)
    )
    if covered and gross > 0:
        esi_ee = ceil0(gross * pct(rules.esi_ee_rate))
        esi_er = ceil0(gross * pct(rules.esi_er_rate))
        slip.deductions.append({"code": "ESI_EE", "name": "ESI", "amount": esi_ee})
        slip.employer.append({"code": "ESI_ER", "name": "Employer ESI", "amount": esi_er})
        slip.workings["esi"] = {"wages": gross, "ee": esi_ee, "er": esi_er}

    # ── professional tax ──
    pt = ZERO
    if emp.pt_override is not None:
        pt = r0(emp.pt_override)
    elif emp.pt_state:
        slabs = rules.pt_slabs.get(emp.pt_state.strip().upper())
        if not slabs:
            slip.warnings.append(
                f"No professional tax slabs are set for {emp.pt_state.upper()}: "
                f"professional tax is 0. Add them in payroll settings."
            )
        else:
            chosen = None
            for slab in slabs:
                if gross >= slab.min_gross:
                    chosen = slab
            if chosen is not None:
                pt = chosen.feb_amount if period.month == 2 and chosen.feb_amount is not None \
                    else chosen.amount
    if pt > 0:
        slip.deductions.append({"code": "PT", "name": "Professional tax", "amount": r0(pt)})

    # ── TDS ──
    taxable_month = r2(
        structure_earned
        + sum((dec(e["amount"]) for e in slip.earnings if e["code"] == "EXTRA" and e["taxable"]),
              ZERO)
    )
    slip.taxable = taxable_month
    tds = ZERO
    if inputs.tds_override is not None:
        tds = r0(inputs.tds_override)
        slip.workings["tax"] = {"method": "set for this month", "monthly": tds}
    elif emp.tds_monthly_override is not None:
        tds = r0(emp.tds_monthly_override)
        slip.workings["tax"] = {"method": "fixed on the employee", "monthly": tds}
    elif tax is None:
        slip.warnings.append("No income tax table is set: TDS is 0. Add one in payroll settings.")
    else:
        if emp.left_on and emp.left_on <= period.end:
            months_after = 0
        elif emp.left_on and emp.left_on <= period.fy_end:
            left = emp.left_on
            months_after = (left.year - period.year) * 12 + left.month - period.month
        else:
            months_after = period.months_left_in_fy()
        projected = dec(so_far.taxable) + taxable_month + full_gross * months_after
        taxable_income = max(ZERO, projected - tax.standard_deduction)
        annual = tax.annual_tax(taxable_income)
        owed = annual["total"] - dec(so_far.tds)
        tds = max(ZERO, r0(owed / Decimal(months_after + 1)))
        slip.workings["tax"] = {
            "method": "new regime, projected over the year",
            "table_fy": fy_label(tax.fy),
            "paid_before": dec(so_far.taxable),
            "this_month": taxable_month,
            "regular_monthly": full_gross,
            "months_after": months_after,
            "projected_salary": r2(projected),
            "standard_deduction": tax.standard_deduction,
            "taxable_income": r2(taxable_income),
            **annual,
            "tds_before": dec(so_far.tds),
            "monthly": tds,
        }
        if tax.fy != period.fy:
            slip.warnings.append(
                f"TDS uses the FY {fy_label(tax.fy)} tax table: none is saved for "
                f"FY {fy_label(period.fy)}."
            )
        elif not tax.confirmed:
            slip.warnings.append(
                f"The FY {fy_label(tax.fy)} tax table has not been confirmed by a processor."
            )
    slip.tds = tds
    if tds > 0:
        slip.deductions.append({"code": "TDS", "name": "Income tax (TDS)", "amount": tds})

    return slip.finish()


def service_years(joined_on: date, as_of: date) -> Decimal:
    if as_of < joined_on:
        return ZERO
    return r2(Decimal((as_of - joined_on).days + 1) / Decimal(365))
