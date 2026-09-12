"""A filed report as a file: one description of it, two renderers.

Somebody wants a report as a PDF to attach to a message, or as a Word file to
paste into something else. Both are the same document, and the failure mode
worth designing against is the obvious one: two renderers written separately
drift, and the Word version quietly stops carrying a section the PDF has.

So the report is turned into a list of **blocks** once — a heading, a table of
tasks, a paragraph of remarks — and the two renderers only know how to draw a
block. Adding a section to the report means adding it to ``blocks`` and it
appears in both. Neither renderer contains a single fact about what a report
is made of.

**Both libraries are already here.** ``reportlab`` for the PDF and
``python-docx`` for the Word file, both pure Python — no native libraries to
install and nothing that behaves differently on Azure than on a laptop, which
is the usual way PDF generation goes wrong in a deployment.

Nothing here decides who may have the file. The route does that, with the same
``may_read`` as the report itself: a file is a copy of the report, and a copy
somebody may not read is not a different question.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Final, Literal

from app.models.report import Report
from app.reports.catalogue import COMPLETION_LABELS, period_label

#: Word's default page is wider than the tables want; these keep a task table
#: from running off the edge in either format, proportionally.
_TASK_WIDTHS: Final = (0.34, 0.13, 0.13, 0.16, 0.12, 0.12)
_ISSUE_WIDTHS: Final = (0.34, 0.13, 0.20, 0.20, 0.13)


@dataclass(slots=True)
class Block:
    """One thing on the page. What ``kind`` is decides which fields are read."""

    kind: Literal["title", "facts", "heading", "prose", "table", "bullets"]
    text: str = ""
    subtitle: str = ""
    #: For ``facts``: label/value pairs, laid out two to a row.
    pairs: list[tuple[str, str]] = field(default_factory=list)
    headers: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    items: list[str] = field(default_factory=list)
    #: Column widths as fractions of the text width. Empty means share evenly.
    widths: tuple[float, ...] = ()


def _text(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return f"{value.quantize(Decimal(1)):f}"
        return f"{value.normalize():f}"
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    return text or "—"


def _labels(report: Report) -> dict[str, str]:
    fields = getattr(report.template, "fields", None) or []
    return {
        f["key"]: (f.get("label") or f["key"])
        for f in fields
        if isinstance(f, dict) and f.get("key")
    }


def file_name(report: Report, suffix: str) -> str:
    """What the file is called once it is on somebody's machine.

    Team, period and author, because the folder it lands in will have several
    of these in it and "report.pdf" is the name that makes the wrong one get
    attached to an email.
    """
    period = period_label(report.cadence, report.period_start, report.period_end)
    parts = [report.team.name, period, report.author.display_name]
    stem = "-".join(
        "".join(ch for ch in part if ch.isalnum() or ch in " -_").strip().replace(" ", "-")
        for part in parts
        if part
    )
    return f"{stem or 'report'}.{suffix}"


def blocks(report: Report) -> list[Block]:
    """The whole report, in order, as things to draw.

    Sections that are empty are said to be empty rather than dropped. A report
    printed for a manager has to be readable as a record of what was and was
    not filled in — a missing issues section reads as "no issues", and only one
    of those is a fact.
    """
    period = period_label(report.cadence, report.period_start, report.period_end)
    out: list[Block] = [
        Block(
            "title",
            text=f"{report.team.name} — {report.cadence} report",
            subtitle=period,
        ),
        Block(
            "facts",
            pairs=[
                ("Filed by", report.author.display_name),
                ("Period", f"{report.period_start} to {report.period_end}"),
                ("Status", report.status),
                ("Filed", _text(report.submitted_at.date() if report.submitted_at else None)),
                (
                    "Template",
                    report.template.name
                    + (f" v{report.template_version}" if report.template_version else ""),
                ),
                ("Scope", report.scope),
            ],
        ),
    ]

    if report.brief:
        out.append(Block("heading", text="In short"))
        if report.brief_headline:
            out.append(Block("prose", text=report.brief_headline))
        out.append(Block("prose", text=report.brief))

    out.append(Block("heading", text="Overview"))
    out.append(Block("prose", text=report.overview or "(nothing was entered)"))

    out.append(Block("heading", text="Tasks"))
    if report.tasks:
        out.append(
            Block(
                "table",
                headers=["Task", "How far", "Status", "Customer", "Quote", "Due"],
                widths=_TASK_WIDTHS,
                rows=[
                    [
                        _text(task.title),
                        COMPLETION_LABELS.get(task.completion, task.completion)
                        + (
                            f" ({task.percent_complete}%)"
                            if task.percent_complete is not None
                            else ""
                        ),
                        _text(task.status),
                        _text(task.end_user),
                        _text(task.quote_no),
                        _text(task.deadline),
                    ]
                    for task in report.tasks
                ],
            )
        )
    else:
        out.append(Block("prose", text="(no tasks were listed)"))

    out.append(Block("heading", text="Issues and blockers"))
    if report.issues:
        out.append(
            Block(
                "table",
                headers=["Issue", "Severity", "Waiting on", "Detail", "State"],
                widths=_ISSUE_WIDTHS,
                rows=[
                    [
                        _text(issue.title),
                        _text(issue.severity),
                        _text(issue.waiting_on),
                        _text(issue.detail),
                        "Resolved" if issue.resolved else "Open",
                    ]
                    for issue in report.issues
                ],
            )
        )
    else:
        out.append(Block("prose", text="(no issues were listed)"))

    if report.metrics:
        out.append(Block("heading", text="Figures"))
        out.append(
            Block(
                "table",
                headers=["Figure", "Value", "Target"],
                rows=[
                    [
                        _text(metric.label),
                        _text(metric.value if metric.value is not None else metric.computed)
                        + (f" {metric.unit}" if metric.unit else ""),
                        _text(metric.target),
                    ]
                    for metric in report.metrics
                ],
            )
        )

    for line in report.project_lines:
        out.append(
            Block("heading", text=f"Project: {line.name}" + (f" ({line.code})" if line.code else ""))
        )
        out.append(
            Block(
                "facts",
                pairs=[
                    ("Overall", _text(line.rag_overall)),
                    ("Scope", _text(line.rag_scope)),
                    ("Cost", _text(line.rag_cost)),
                    ("Schedule", _text(line.rag_schedule)),
                    ("Benefits", _text(line.rag_benefits)),
                    ("Complete", f"{line.percent_complete}%"),
                    (
                        "Tasks",
                        f"{line.tasks_done}/{line.tasks_total} done, "
                        f"{line.tasks_blocked} blocked, {line.tasks_overdue} overdue",
                    ),
                    (
                        "Milestones",
                        f"{line.milestones_done}/{line.milestones_total} done, "
                        f"{line.milestones_overdue} overdue",
                    ),
                ],
            )
        )
        milestones = getattr(line, "milestones", None) or []
        if milestones:
            out.append(
                Block(
                    "table",
                    headers=["Milestone", "Due", "Complete", "Plan"],
                    rows=[
                        [
                            _text(m.name),
                            _text(m.due_on),
                            f"{m.percent_complete}%",
                            _text(m.plan),
                        ]
                        for m in milestones
                    ],
                )
            )
        if line.activities:
            out.append(Block("prose", text=f"Activities: {line.activities}"))
        if line.action_required:
            out.append(Block("prose", text=f"Action required: {line.action_required}"))

    labels = _labels(report)
    answers = [
        (labels.get(key, key), _text(value))
        for key, value in (report.answers or {}).items()
        if value not in (None, "", [], {})
    ]
    if answers:
        out.append(Block("heading", text="This team's own questions"))
        out.append(Block("facts", pairs=answers))

    out.append(Block("heading", text="Remarks"))
    out.append(Block("prose", text=report.remarks or "(nothing was entered)"))

    out.append(Block("heading", text="Summary"))
    out.append(Block("prose", text=report.summary or "(nothing was entered)"))
    return out


# ── PDF ────────────────────────────────────────────────────────────────


def render_pdf(report: Report) -> bytes:
    """The blocks, as A4 pages.

    Built with platypus rather than drawn at coordinates, so a long task list
    flows onto a second page and a table that crosses a page break repeats its
    header instead of losing it.
    """
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        KeepTogether,
        PageBreak,  # noqa: F401 - kept for future section breaks
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    ink = colors.HexColor("#16181d")
    muted = colors.HexColor("#5c6370")
    line = colors.HexColor("#e2e5ea")

    base = getSampleStyleSheet()
    styles = {
        "title": ParagraphStyle(
            "t", parent=base["Title"], fontSize=17, leading=21, alignment=TA_LEFT,
            textColor=ink, spaceAfter=2,
        ),
        "subtitle": ParagraphStyle(
            "s", parent=base["Normal"], fontSize=10, leading=13, textColor=muted,
            spaceAfter=10,
        ),
        "heading": ParagraphStyle(
            "h", parent=base["Heading2"], fontSize=11.5, leading=15, textColor=ink,
            spaceBefore=12, spaceAfter=5,
        ),
        "body": ParagraphStyle(
            "b", parent=base["Normal"], fontSize=9.5, leading=13.5, textColor=ink,
        ),
        "cell": ParagraphStyle(
            "c", parent=base["Normal"], fontSize=8.5, leading=11, textColor=ink,
        ),
        "head": ParagraphStyle(
            "ch", parent=base["Normal"], fontSize=8, leading=10, textColor=muted,
        ),
    }

    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=16 * mm, bottomMargin=16 * mm,
        title=file_name(report, "pdf"),
        author=report.author.display_name,
    )
    width = document.width

    def cells(values: list[str], style: str) -> list[Paragraph]:
        # Every cell is a Paragraph rather than a string: a long task title has
        # to wrap inside its column, and a plain string does not.
        return [Paragraph(_escape(value), styles[style]) for value in values]

    story: list[Any] = []
    for block in blocks(report):
        if block.kind == "title":
            story.append(Paragraph(_escape(block.text), styles["title"]))
            story.append(Paragraph(_escape(block.subtitle), styles["subtitle"]))
        elif block.kind == "heading":
            story.append(Paragraph(_escape(block.text), styles["heading"]))
        elif block.kind == "prose":
            story.append(Paragraph(_escape(block.text), styles["body"]))
            story.append(Spacer(1, 4))
        elif block.kind == "facts":
            rows = [
                cells([f"{label}: {value}" for label, value in block.pairs[i : i + 2]], "cell")
                for i in range(0, len(block.pairs), 2)
            ]
            for row in rows:
                while len(row) < 2:
                    row.append(Paragraph("", styles["cell"]))
            table = Table(rows, colWidths=[width / 2] * 2, hAlign="LEFT")
            table.setStyle(
                TableStyle([
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ])
            )
            story.append(table)
            story.append(Spacer(1, 4))
        elif block.kind == "table":
            widths = (
                [width * w for w in block.widths]
                if block.widths
                else [width / max(1, len(block.headers))] * len(block.headers)
            )
            data = [cells(block.headers, "head")] + [cells(row, "cell") for row in block.rows]
            table = Table(data, colWidths=widths, hAlign="LEFT", repeatRows=1)
            table.setStyle(
                TableStyle([
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LINEBELOW", (0, 0), (-1, 0), 0.6, line),
                    ("LINEBELOW", (0, 1), (-1, -2), 0.3, line),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ])
            )
            story.append(KeepTogether([table]) if len(block.rows) <= 4 else table)
            story.append(Spacer(1, 4))
        elif block.kind == "bullets":
            for item in block.items:
                story.append(Paragraph(f"• {_escape(item)}", styles["body"]))

    document.build(story)
    return buffer.getvalue()


def _escape(value: str) -> str:
    """Reportlab reads a paragraph as its own small markup, so `<` has to go.

    A task called "<40% margin" would otherwise be an unclosed tag and take the
    rest of the document with it.
    """
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", "<br/>")
    )


# ── Word ───────────────────────────────────────────────────────────────


def render_docx(report: Report) -> bytes:
    """The same blocks, as a .docx somebody can edit.

    Word rather than "a doc": a .doc is a format Microsoft stopped writing two
    decades ago, and what people mean when they ask for one is a file that
    opens in Word and can be pasted from.
    """
    from docx import Document
    from docx.shared import Pt

    document = Document()
    for style, size in (("Normal", 9.5), ("Heading 1", 16), ("Heading 2", 12)):
        try:
            document.styles[style].font.size = Pt(size)
        except KeyError:  # pragma: no cover - a template without that style
            pass

    for block in blocks(report):
        if block.kind == "title":
            document.add_heading(block.text, level=1)
            if block.subtitle:
                document.add_paragraph(block.subtitle)
        elif block.kind == "heading":
            document.add_heading(block.text, level=2)
        elif block.kind == "prose":
            document.add_paragraph(block.text)
        elif block.kind == "facts":
            table = document.add_table(rows=0, cols=2)
            table.style = "Table Grid"
            for index in range(0, len(block.pairs), 2):
                cells = table.add_row().cells
                for column, (label, value) in enumerate(block.pairs[index : index + 2]):
                    cells[column].text = f"{label}: {value}"
        elif block.kind == "table":
            table = document.add_table(rows=1, cols=len(block.headers))
            table.style = "Table Grid"
            for column, header in enumerate(block.headers):
                cell = table.rows[0].cells[column]
                cell.text = header
                for run in cell.paragraphs[0].runs:
                    run.bold = True
            for row in block.rows:
                cells = table.add_row().cells
                for column, value in enumerate(row):
                    cells[column].text = value
            document.add_paragraph()
        elif block.kind == "bullets":
            for item in block.items:
                document.add_paragraph(item, style="List Bullet")

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


#: What each format is called on the way out.
FORMATS: Final[dict[str, tuple[str, str]]] = {
    "pdf": ("pdf", "application/pdf"),
    "docx": (
        "docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
}


def render(report: Report, fmt: str) -> tuple[bytes, str, str]:
    """The file, its name and its content type."""
    if fmt not in FORMATS:
        raise ValueError(f"format must be one of: {', '.join(FORMATS)}")
    suffix, media_type = FORMATS[fmt]
    content = render_pdf(report) if fmt == "pdf" else render_docx(report)
    return content, file_name(report, suffix), media_type
