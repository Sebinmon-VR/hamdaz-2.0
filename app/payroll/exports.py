"""The files a payroll month becomes: payslips, the register, and what the bank takes.

All of these are renderings of the stored lines and nothing else. No figure is
recomputed here, so the payslip, the register and the bank file for a month
cannot disagree with each other or with the screen.

**Bank files are only for approved months** — the router enforces it, and the
reason is the approval itself: a SIF file produced from a draft is a payment
nobody signed off.

The WPS SIF layout is the Central Bank's common one (EDR lines per employee,
one SCR control line). Banks and exchange houses accept it, but some add their
own wrapper or column; the first file for a new bank is worth showing to it
before it is uploaded for real.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.models.payroll import Country, PayrollLine, PayrollRun, PayrollSettings, RunStatus
from app.payroll.rules import Period, dec, r0

PDF_TYPE: Final = "application/pdf"
XLSX_TYPE: Final = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
LOGO: Final = Path(__file__).resolve().parent.parent / "quoting" / "assets" / "logo_mark.png"

NAVY = colors.HexColor("#0e5e80")
INK = colors.HexColor("#22303f")
MUTED = colors.HexColor("#64727f")
LINE = colors.HexColor("#e2e9ef")
ROW = colors.HexColor("#f5f9fb")


class ExportError(Exception):
    """The file cannot be produced — usually missing bank or statutory details."""


def _period(run: PayrollRun) -> Period:
    return Period(run.year, run.month)


def money(value: Any) -> str:
    return f"{dec(value):,.2f}"


def _mask(value: str | None) -> str:
    if not value:
        return "—"
    return f"•••• {value[-4:]}" if len(value) > 4 else value


def file_stem(run: PayrollRun) -> str:
    country = "UAE" if run.country == Country.UAE else "India"
    return f"Payroll {country} {run.year}-{run.month:02d}"


# ── payslips ──────────────────────────────────────────────────────────


def _styles() -> dict[str, ParagraphStyle]:
    base = ParagraphStyle("base", fontName="Helvetica", fontSize=8.5, leading=11, textColor=INK)
    return {
        "base": base,
        "muted": ParagraphStyle("muted", parent=base, textColor=MUTED, fontSize=7.5, leading=10),
        "org": ParagraphStyle("org", parent=base, fontName="Helvetica-Bold", fontSize=11, leading=14),
        "title": ParagraphStyle(
            "title", parent=base, fontName="Helvetica-Bold", fontSize=13, leading=16,
            textColor=NAVY, alignment=TA_RIGHT,
        ),
        "right": ParagraphStyle("right", parent=base, alignment=TA_RIGHT),
        "bold": ParagraphStyle("bold", parent=base, fontName="Helvetica-Bold"),
        "bold_right": ParagraphStyle(
            "bold_right", parent=base, fontName="Helvetica-Bold", alignment=TA_RIGHT
        ),
    }


def _company(settings: PayrollSettings, country: str) -> tuple[str, str | None]:
    if country == Country.UAE:
        return settings.ae_company_name, settings.ae_company_address
    return settings.in_company_name, settings.in_company_address


def _payslip_story(
    run: PayrollRun, line: PayrollLine, settings: PayrollSettings, s: dict[str, ParagraphStyle]
) -> list[Any]:
    period = _period(run)
    name, address = _company(settings, run.country)
    ids = line.identifiers or {}
    cur = run.currency

    header_left: list[Any] = [Paragraph(name, s["org"])]
    if address:
        header_left.append(Paragraph(address.replace("\n", "<br/>"), s["muted"]))
    logo = Image(str(LOGO), width=14 * mm, height=14 * mm) if LOGO.exists() else ""
    title = [Paragraph("Payslip", s["title"]), Paragraph(period.label, s["right"])]
    if run.status in (RunStatus.DRAFT, RunStatus.SUBMITTED):
        title.append(Paragraph(f"<font color='#d62d7d'><b>{run.status.upper()} — not approved</b></font>", s["right"]))
    head = Table([[logo, header_left, title]], colWidths=[18 * mm, 95 * mm, 67 * mm])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))

    def kv(label: str, value: Any) -> list[Any]:
        return [Paragraph(label, s["muted"]), Paragraph(str(value if value not in (None, "") else "—"), s["base"])]

    left = [
        kv("Employee", line.employee_name),
        kv("Code", line.employee_code),
        kv("Designation", line.designation),
        kv("Department", ids.get("department")),
        kv("Joined", ids.get("joined_on")),
    ]
    if run.country == Country.UAE:
        right = [
            kv("Bank", ids.get("bank_name")),
            kv("IBAN", _mask(ids.get("iban"))),
            kv("Labour person id", ids.get("mol_person_id")),
        ]
    else:
        right = [
            kv("Bank", ids.get("bank_name")),
            kv("Account", _mask(ids.get("bank_account"))),
            kv("PAN", ids.get("pan")),
            kv("UAN", ids.get("uan")),
        ]
    right.append(kv("Days paid", f"{dec(line.paid_days).normalize():f} of {dec(line.basis_days).normalize():f}"))
    rows = max(len(left), len(right))
    left += [["", ""]] * (rows - len(left))
    right += [["", ""]] * (rows - len(right))
    details = Table(
        [a + b for a, b in zip(left, right, strict=True)], colWidths=[26 * mm, 64 * mm, 30 * mm, 60 * mm]
    )
    details.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.5, LINE),
                ("BACKGROUND", (0, 0), (-1, -1), ROW),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )

    earnings = [[Paragraph("<b>Earnings</b>", s["base"]), Paragraph("<b>Monthly</b>", s["right"]),
                 Paragraph("<b>This month</b>", s["right"])]]
    for e in line.earnings:
        earnings.append(
            [Paragraph(e["name"], s["base"]), Paragraph(money(e.get("full", e["amount"])), s["right"]),
             Paragraph(money(e["amount"]), s["right"])]
        )
    earnings.append([Paragraph("<b>Gross pay</b>", s["base"]), "", Paragraph(f"<b>{money(line.gross)}</b>", s["right"])])

    deductions = [[Paragraph("<b>Deductions</b>", s["base"]), Paragraph("<b>Amount</b>", s["right"])]]
    for e in line.deductions:
        deductions.append([Paragraph(e["name"], s["base"]), Paragraph(money(e["amount"]), s["right"])])
    if len(deductions) == 1:
        deductions.append([Paragraph("None", s["muted"]), ""])
    deductions.append(
        [Paragraph("<b>Total deductions</b>", s["base"]),
         Paragraph(f"<b>{money(line.total_deductions)}</b>", s["right"])]
    )

    def boxed(table: Table) -> Table:
        table.setStyle(
            TableStyle(
                [
                    ("LINEBELOW", (0, 0), (-1, 0), 0.6, NAVY),
                    ("LINEABOVE", (0, -1), (-1, -1), 0.4, LINE),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )
        return table

    pay = Table(
        [[boxed(Table(earnings, colWidths=[50 * mm, 22 * mm, 24 * mm])),
          boxed(Table(deductions, colWidths=[54 * mm, 26 * mm]))]],
        colWidths=[98 * mm, 82 * mm],
    )
    pay.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))

    net = Table(
        [[Paragraph("<b>Net pay</b>", s["base"]), Paragraph(f"<b>{cur} {money(line.net)}</b>", s["bold_right"])]],
        colWidths=[120 * mm, 60 * mm],
    )
    net.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#eef8fd")),
                ("BOX", (0, 0), (-1, -1), 0.6, NAVY),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )

    story: list[Any] = [head, Spacer(1, 6 * mm), details, Spacer(1, 5 * mm), pay, Spacer(1, 5 * mm), net]
    if run.country == Country.INDIA and line.employer:
        employer = ", ".join(f"{e['name']} {money(e['amount'])}" for e in line.employer)
        story += [Spacer(1, 3 * mm), Paragraph(f"Employer contributions (not deducted from pay): {employer}", s["muted"])]
    story += [
        Spacer(1, 8 * mm),
        Paragraph(
            f"All amounts in {cur}. This payslip is generated by the Hamdaz ERP and needs no signature.",
            s["muted"],
        ),
    ]
    return story


def payslips_pdf(run: PayrollRun, lines: list[PayrollLine], settings: PayrollSettings) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=14 * mm,
        bottomMargin=14 * mm, title=f"Payslips {_period(run).label}", author=_company(settings, run.country)[0],
    )
    s = _styles()
    story: list[Any] = []
    for index, line in enumerate(lines):
        if index:
            story.append(PageBreak())
        story += _payslip_story(run, line, settings, s)
    if not story:
        story = [Paragraph("Nobody in this month.", s["base"])]
    doc.build(story)
    return buffer.getvalue()


# ── the register ──────────────────────────────────────────────────────


def _by_name(items: list[dict[str, Any]]) -> dict[str, Decimal]:
    """Amounts by line name, summed — two extras may share a name."""
    totals: dict[str, Decimal] = {}
    for item in items:
        totals[item["name"]] = totals.get(item["name"], Decimal("0")) + dec(item["amount"])
    return totals


def register_xlsx(run: PayrollRun, settings: PayrollSettings) -> bytes:
    """One row per person, a column per earning and deduction, totals at the foot."""
    lines = list(run.lines)

    def names(attr: str) -> list[str]:
        seen: dict[str, None] = {}
        for line in lines:
            for item in getattr(line, attr):
                seen.setdefault(item["name"], None)
        return list(seen)

    earning_cols = names("earnings")
    deduction_cols = names("deductions")
    employer_cols = names("employer")

    wb = Workbook()
    ws = wb.active
    ws.title = f"{run.year}-{run.month:02d}"
    company, _ = _company(settings, run.country)
    ws.append([f"{company} — payroll register, {_period(run).label} ({run.currency}) — {run.status}"])
    ws["A1"].font = Font(bold=True, size=12)
    ws.append([])

    bank_cols = (
        ["Bank", "IBAN", "Routing code", "Labour person id"]
        if run.country == Country.UAE
        else ["Bank", "Account", "IFSC", "PAN", "UAN", "ESI number"]
    )
    header = (
        ["Code", "Name", "Designation", "Paid days", "Unpaid days"]
        + earning_cols + ["Gross"]
        + deduction_cols + ["Total deductions", "Net pay"]
        + employer_cols + ["Employer cost"]
        + bank_cols + ["On hold", "Notes"]
    )
    ws.append(header)
    header_row = ws.max_row
    for cell in ws[header_row]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="0E5E80")
        cell.alignment = Alignment(wrap_text=True, vertical="top")

    first = header_row + 1
    for line in lines:
        e, d, er = (_by_name(getattr(line, attr)) for attr in ("earnings", "deductions", "employer"))
        ids = line.identifiers or {}
        bank = (
            [ids.get("bank_name"), ids.get("iban"), ids.get("bank_routing"), ids.get("mol_person_id")]
            if run.country == Country.UAE
            else [ids.get("bank_name"), ids.get("bank_account"), ids.get("ifsc"), ids.get("pan"),
                  ids.get("uan"), ids.get("esi_number")]
        )
        ws.append(
            [line.employee_code, line.employee_name, line.designation,
             float(line.paid_days), float(line.unpaid_days)]
            + [float(e.get(c, 0)) for c in earning_cols] + [float(line.gross)]
            + [float(d.get(c, 0)) for c in deduction_cols]
            + [float(line.total_deductions), float(line.net)]
            + [float(er.get(c, 0)) for c in employer_cols] + [float(line.employer_cost)]
            + bank + ["Yes" if line.on_hold else "", line.note or ""]
        )
    last = ws.max_row
    money_from = 6
    money_to = 5 + len(earning_cols) + 1 + len(deduction_cols) + 2 + len(employer_cols) + 1
    if lines:
        total_row = ["Total", f"{len(lines)} people", "", "", ""]
        for col in range(money_from, money_to + 1):
            letter = get_column_letter(col)
            total_row.append(f"=SUM({letter}{first}:{letter}{last})")
        ws.append(total_row)
        for cell in ws[ws.max_row]:
            cell.font = Font(bold=True)
    for row in ws.iter_rows(min_row=first, max_row=ws.max_row, min_col=money_from, max_col=money_to):
        for cell in row:
            cell.number_format = "#,##0.00"
    widths = {1: 12, 2: 28, 3: 22}
    for col in range(1, len(header) + 1):
        ws.column_dimensions[get_column_letter(col)].width = widths.get(col, 14)
    ws.freeze_panes = ws.cell(row=first, column=3)

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


# ── bank files ────────────────────────────────────────────────────────


def _payable(run: PayrollRun) -> list[PayrollLine]:
    return [ln for ln in run.lines if not ln.on_hold and dec(ln.net) > 0]


def _variable_pay(line: PayrollLine) -> Decimal:
    return sum((dec(e["amount"]) for e in line.earnings if e.get("variable")), Decimal("0"))


def wps_sif(run: PayrollRun, settings: PayrollSettings, now: datetime | None = None) -> tuple[str, str]:
    """(file name, contents) of the UAE WPS salary information file."""
    if run.country != Country.UAE:
        raise ExportError("A WPS file is for UAE payroll.")
    employer = settings.ae_employer_mol_id
    bank = settings.ae_employer_bank_routing
    if not employer or not bank:
        raise ExportError(
            "Set the employer's 13-digit MOHRE id and the paying bank's 9-digit routing "
            "code in payroll settings first."
        )
    period = _period(run)
    lines = _payable(run)
    missing: list[str] = []
    rows: list[str] = []
    total = Decimal("0")
    for line in lines:
        ids = line.identifiers or {}
        person, routing, iban = ids.get("mol_person_id"), ids.get("bank_routing"), ids.get("iban")
        gaps = [label for label, value in (("labour person id", person), ("routing code", routing),
                                           ("IBAN", iban)) if not value]
        if gaps:
            missing.append(f"{line.employee_name}: {', '.join(gaps)}")
            continue
        joined = date.fromisoformat(ids["joined_on"]) if ids.get("joined_on") else period.start
        left = date.fromisoformat(ids["left_on"]) if ids.get("left_on") else period.end
        start, end = max(period.start, joined), min(period.end, left)
        variable = min(_variable_pay(line), dec(line.net))
        fixed = dec(line.net) - variable
        leave_days = r0((line.workings or {}).get("days", {}).get("lop", 0))
        rows.append(
            ",".join(
                [
                    "EDR", person, routing, iban, start.isoformat(), end.isoformat(),
                    str((end - start).days + 1), f"{fixed:.2f}", f"{variable:.2f}", f"{leave_days}",
                ]
            )
        )
        total += dec(line.net)
    if missing:
        raise ExportError("Cannot make the WPS file. Missing details — " + "; ".join(missing))
    if not rows:
        raise ExportError("Nobody to pay in this file (everyone is on hold or has no net pay).")
    stamp = (now or datetime.now(ZoneInfo("Asia/Dubai")))
    reference = (settings.ae_employer_reference or "").replace(",", " ")
    rows.append(
        ",".join(
            [
                "SCR", employer, bank, stamp.date().isoformat(), stamp.strftime("%H%M"),
                f"{run.month:02d}{run.year}", str(len(rows)), f"{total:.2f}", "AED", reference,
            ]
        )
    )
    name = f"{employer}{stamp.strftime('%y%m%d%H%M%S')}.SIF"
    return name, "\r\n".join(rows) + "\r\n"


def bank_csv(run: PayrollRun) -> str:
    """India: a plain transfer sheet — name, account, IFSC, amount — for the bank's bulk upload."""
    if run.country != Country.INDIA:
        raise ExportError("The bank sheet is for India payroll; the UAE uses the WPS file.")
    missing: list[str] = []
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Beneficiary name", "Account number", "IFSC", "Amount", "Narration"])
    narration = f"Salary {_period(run).label}"
    for line in _payable(run):
        ids = line.identifiers or {}
        if not ids.get("bank_account") or not ids.get("ifsc"):
            missing.append(line.employee_name)
            continue
        writer.writerow([line.employee_name, ids["bank_account"], ids["ifsc"], f"{dec(line.net):.2f}", narration])
    if missing:
        raise ExportError("Bank account or IFSC missing for: " + ", ".join(missing))
    return out.getvalue()


def pf_ecr(run: PayrollRun) -> str:
    """India: the EPFO ECR text file — one ``#~#``-separated line per member."""
    if run.country != Country.INDIA:
        raise ExportError("The PF ECR file is for India payroll.")
    rows: list[str] = []
    missing: list[str] = []
    for line in run.lines:
        pf = (line.workings or {}).get("pf")
        if not pf:
            continue
        uan = (line.identifiers or {}).get("uan")
        if not uan:
            missing.append(line.employee_name)
            continue
        rows.append(
            "#~#".join(
                str(v)
                for v in (
                    uan,
                    line.employee_name.upper(),
                    r0(line.gross),
                    r0(pf["epf_wages"]),
                    r0(pf["eps_wages"]),
                    r0(pf["edli_wages"]),
                    r0(pf["ee"]),
                    r0(pf["eps"]),
                    r0(pf["er_epf"]),
                    r0(line.unpaid_days),
                    0,
                )
            )
        )
    if missing:
        raise ExportError("UAN missing for: " + ", ".join(missing))
    if not rows:
        raise ExportError("Nobody in this month contributes to PF.")
    return "\n".join(rows) + "\n"
