"""The shape every report has, and the figures that fill themselves in.

Two things live here and nothing else: the six sections a report is made of,
and the metrics that can be worked out from a person's tasks rather than typed.

**Why the sections are code.** A team's questions are data — a super admin adds
"quotes sent" to the presales weekly without a deploy. The *sections* are not,
because their whole value is being the same everywhere. A manager reading four
teams' reports on a Monday morning should find the issues in the same place
each time; a CEO asking "what is blocking us" should get an answer that spans
teams. Both stop being possible the moment a team can rename its issues section
to "challenges" and another can drop it.

So: the frame is fixed, what hangs in it is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Callable, Final, Iterable

from app.models.report import ReportCadence
from app.models.templates import FieldType

# ── the six sections ───────────────────────────────────────────────────

OVERVIEW: Final = "overview"
TASKS: Final = "tasks"
ISSUES: Final = "issues"
REMARKS: Final = "remarks"
METRICS: Final = "metrics"
SUMMARY: Final = "summary"


@dataclass(frozen=True, slots=True)
class Section:
    key: str
    name: str
    #: For the person filling it in, and for the model deciding what to put there.
    description: str
    #: ``prose`` is one text box; ``rows`` is a list the author adds to;
    #: ``figures`` is the metric grid. A frontend renders from this and the
    #: template's own fields, and needs to know nothing else.
    kind: str


SECTIONS: Final[tuple[Section, ...]] = (
    Section(
        OVERVIEW,
        "Overview",
        "What the period was about, in a few lines. Written first and read "
        "first: somebody with thirty seconds should get the gist from this "
        "alone.",
        "prose",
    ),
    Section(
        TASKS,
        "Tasks",
        "The work itself, one row each, with where it stands. Rows can be "
        "pulled from the Proposals list — which brings the title, status, "
        "deadline, the link and its attachments — or typed in for work that "
        "lives nowhere else. Either way the author says how far along it is, "
        "because the list says whether SharePoint was updated and that is a "
        "different claim from whether the work is done.",
        "rows",
    ),
    Section(
        ISSUES,
        "Issues",
        "What is in the way, one row each, with how bad it is and who it waits "
        "on. Kept apart from remarks on purpose: this is the part a manager is "
        "meant to act on.",
        "rows",
    ),
    Section(
        REMARKS,
        "Remarks",
        "Anything worth saying that is not a task and not a blocker — context, "
        "a warning, something noticed.",
        "prose",
    ),
    Section(
        METRICS,
        "Metrics",
        "The numbers. Those that can be counted from the tasks are filled in "
        "already and can be corrected; the rest the team types.",
        "figures",
    ),
    Section(
        SUMMARY,
        "Summary",
        "What the reader should take away, and what happens next. One or two "
        "sentences.",
        "prose",
    ),
)

SECTIONS_BY_KEY: Final[dict[str, Section]] = {s.key: s for s in SECTIONS}

#: The sections a template may add its own fields to. All of them: a team may
#: want a select in its issues section as readily as a number in its metrics.
SECTION_KEYS: Final[tuple[str, ...]] = tuple(s.key for s in SECTIONS)


# ── what a period is ───────────────────────────────────────────────────


def period_for(cadence: str, on: date) -> tuple[date, date]:
    """The period a report covers, from the cadence and any day inside it.

    Inclusive at both ends, so a daily report's two dates are the same day.
    Weeks run Monday to Sunday — the ISO week, so that "week 41" means the same
    thing here as it does in every calendar the company already uses.

    An ad-hoc report has no period this can know, so it is given the single day
    and the caller is expected to overwrite both ends.
    """
    if cadence == ReportCadence.WEEKLY:
        start = on - timedelta(days=on.weekday())
        return start, start + timedelta(days=6)
    if cadence == ReportCadence.MONTHLY:
        start = on.replace(day=1)
        next_month = (start + timedelta(days=32)).replace(day=1)
        return start, next_month - timedelta(days=1)
    return on, on


def period_label(cadence: str, start: date, end: date) -> str:
    """How a period reads to a person. Used in titles and by the assistant."""
    if cadence == ReportCadence.DAILY:
        return start.strftime("%A %d %B %Y")
    if cadence == ReportCadence.WEEKLY:
        return f"week of {start.strftime('%d %B %Y')}"
    if cadence == ReportCadence.MONTHLY:
        return start.strftime("%B %Y")
    if start == end:
        return start.isoformat()
    return f"{start.isoformat()} to {end.isoformat()}"


# ── the figures that fill themselves in ────────────────────────────────


@dataclass(frozen=True, slots=True)
class ComputedMetric:
    """A number worked out from the report's own task rows.

    Deliberately computed from the *report's* tasks and not from SharePoint
    directly. The author decides which tasks the report is about — they may drop
    a row that is not theirs, or add one the list does not know — and a metric
    that disagreed with the list of tasks printed directly above it would be
    read as a bug, whatever it was measuring.
    """

    key: str
    label: str
    unit: str | None
    description: str
    of: Callable[[Iterable], int]


def _count(predicate) -> Callable[[Iterable], int]:
    return lambda lines: sum(1 for line in lines if predicate(line))


#: Note that "overdue" is judged on the deadline the row carried, not on
#: SharePoint's status. A bid whose closing date has passed and which is not
#: marked done is late whatever the list says about it — that is the whole
#: reason the Proposals module derives an effective status rather than trusting
#: the stored one.
COMPUTED_METRICS: Final[tuple[ComputedMetric, ...]] = (
    ComputedMetric(
        "tasks_total", "Tasks on this report", None,
        "Every row in the tasks section, pulled or typed.",
        _count(lambda line: True),
    ),
    ComputedMetric(
        "tasks_done", "Completed", None,
        "Rows the author marked done.",
        _count(lambda line: line.completion == "done"),
    ),
    ComputedMetric(
        "tasks_in_progress", "In progress", None,
        "Rows being worked on.",
        _count(lambda line: line.completion == "in_progress"),
    ),
    ComputedMetric(
        "tasks_blocked", "Blocked", None,
        "Rows the author marked blocked. Read next to the issues section.",
        _count(lambda line: line.completion == "blocked"),
    ),
    ComputedMetric(
        "tasks_overdue", "Overdue", None,
        "Not done, and the deadline has passed.",
        _count(
            lambda line: line.completion != "done"
            and line.deadline is not None
            and line.deadline < date.today()
        ),
    ),
    ComputedMetric(
        "tasks_with_attachments", "With attachments", None,
        "Rows carrying files in SharePoint. A rough read on how much of the "
        "work has left a trace anybody else can pick up.",
        _count(lambda line: bool(line.has_attachments)),
    ),
)

COMPUTED_BY_KEY: Final[dict[str, ComputedMetric]] = {m.key: m for m in COMPUTED_METRICS}


def compute(lines: Iterable) -> dict[str, Decimal]:
    """Every computed metric, over one report's task rows."""
    rows = list(lines)
    return {metric.key: Decimal(metric.of(rows)) for metric in COMPUTED_METRICS}


# ── how far along a task is ────────────────────────────────────────────

#: What the author may say about a row. Short and closed, because the value of
#: this field is that it means the same on every report; a free-text "status"
#: is what the Proposals list already has, and counting it is what nobody can do.
COMPLETIONS: Final[tuple[str, ...]] = (
    "not_started", "in_progress", "blocked", "done", "dropped",
)

COMPLETION_LABELS: Final[dict[str, str]] = {
    "not_started": "Not started",
    "in_progress": "In progress",
    "blocked": "Blocked",
    "done": "Done",
    #: Not a failure. Work that stopped being worth doing is worth saying so
    #: about, and lumping it in with "done" hides it.
    "dropped": "Dropped",
}


# ── templates ──────────────────────────────────────────────────────────

#: What the reports module asks the templates module for. One kind, several
#: templates — presales' daily, presales' weekly, purchasing's daily — and a
#: schedule row decides which team gets which. The same arrangement HR uses for
#: job applications, for the same reason: the module should not have to know
#: which particular template an admin made.
REPORT_KIND: Final = "team_report"

#: The template a team falls back to when nobody has set up a schedule. Plain
#: sections and no extra questions, so a team can file a report the day they are
#: created rather than waiting on an administrator.
GENERIC_REPORT: Final = "team_report"

PRESALES_DAILY: Final = "report_presales_daily"
PRESALES_WEEKLY: Final = "report_presales_weekly"


def section_specs() -> list[dict[str, str]]:
    """The six sections, in the shape ``FormTemplate.sections`` holds.

    Built rather than written out so the template and the skeleton cannot
    disagree: a section added here appears on every report template the next
    time one is seeded, and there is no second list to remember.
    """
    return [
        {"key": s.key, "name": s.name, "help": s.description} for s in SECTIONS
    ]


def _field(
    key: str,
    label: str,
    kind: FieldType,
    *,
    section: str,
    required: bool = False,
    help: str | None = None,
    options: list[str] | None = None,
) -> dict[str, Any]:
    """One extra question a team is asked, on top of the six sections.

    The same field shape the rest of the app uses, so the existing template
    editor renders and edits these without knowing they belong to a report.
    """
    spec: dict[str, Any] = {
        "key": key,
        "label": label,
        "type": kind.value,
        "section": section,
        "required": required,
    }
    if help:
        spec["help"] = help
    if options:
        spec["options"] = options
    return spec


#: What presales is asked at the end of a day, beyond the six sections.
#:
#: Short on purpose. A daily report that takes twenty minutes is a daily report
#: that gets filed for a fortnight and then stops, and the tasks section already
#: carries the work itself — these are the few numbers that are not derivable
#: from it and that presales are actually measured on.
PRESALES_DAILY_FIELDS: Final[list[dict[str, Any]]] = [
    _field(
        "quotes_sent", "Quotes sent today", FieldType.NUMBER, section=METRICS,
        help="Customer quotes that actually went out, not ones drafted.",
    ),
    _field(
        "enquiries_received", "New enquiries", FieldType.NUMBER, section=METRICS,
        help="Enquiries that arrived today, however they arrived.",
    ),
    _field(
        "bids_submitted", "Bids submitted", FieldType.NUMBER, section=METRICS,
        help="Submitted to the portal or to the customer.",
    ),
    _field(
        "supplier_followups", "Supplier follow-ups", FieldType.NUMBER, section=METRICS,
        help="Chases for pricing. The number that explains a slow week.",
    ),
    _field(
        "customer_visits", "Customer meetings", FieldType.NUMBER, section=METRICS,
    ),
    _field(
        "support_needed", "Support needed", FieldType.SELECT, section=ISSUES,
        options=["none", "pricing approval", "technical input", "supplier contact",
                 "customer escalation", "other"],
        help="What would move things along fastest. Read by whoever gets this.",
    ),
    _field(
        "tomorrow_focus", "Focus tomorrow", FieldType.TEXTAREA, section=SUMMARY,
        help="The one or two things being picked up first.",
    ),
]

#: The weekly asks for the same figures over a week, plus the judgements a day
#: is too short to support: what the pipeline looks like, and what was won or
#: lost. Those are the questions a manager reads a weekly to answer, and asking
#: them daily would produce noise rather than an answer.
PRESALES_WEEKLY_FIELDS: Final[list[dict[str, Any]]] = [
    _field(
        "quotes_sent", "Quotes sent this week", FieldType.NUMBER, section=METRICS,
        required=True,
    ),
    _field(
        "quote_value", "Value quoted", FieldType.CURRENCY, section=METRICS,
        help="Total of what went out this week, in AED.",
    ),
    _field(
        "bids_submitted", "Bids submitted", FieldType.NUMBER, section=METRICS,
    ),
    _field(
        "bids_won", "Won", FieldType.NUMBER, section=METRICS,
        help="Confirmed this week, whenever the quote went out.",
    ),
    _field(
        "bids_lost", "Lost", FieldType.NUMBER, section=METRICS,
        help="Worth recording honestly. A week with no losses usually means "
             "nobody asked the customer.",
    ),
    _field(
        "loss_reasons", "Why they were lost", FieldType.TEXTAREA, section=ISSUES,
        help="Price, lead time, specification, no reason given. The one field "
             "here that changes what the company does next.",
    ),
    _field(
        "pipeline_value", "Live pipeline", FieldType.CURRENCY, section=METRICS,
        help="Everything still open at the end of the week, in AED.",
    ),
    _field(
        "closing_next_week", "Closing next week", FieldType.NUMBER, section=METRICS,
        help="Bids whose closing date falls in the coming week.",
    ),
    _field(
        "support_needed", "Support needed", FieldType.SELECT, section=ISSUES,
        options=["none", "pricing approval", "technical input", "supplier contact",
                 "customer escalation", "more capacity", "other"],
    ),
    _field(
        "next_week_focus", "Focus next week", FieldType.TEXTAREA, section=SUMMARY,
        required=True,
    ),
]


#: The report templates the product ships with. Seeded like the form catalogue:
#: code, not user data, because the reports module refers to the generic one by
#: key. A super admin edits any of it afterwards and the seed will not undo it.
TEMPLATES: Final[tuple[dict[str, Any], ...]] = (
    {
        "key": GENERIC_REPORT,
        "name": "Team report",
        "description": (
            "The six sections and nothing else. What a team files until "
            "somebody writes one for them, so a new team can report from the "
            "day it exists rather than waiting on an administrator."
        ),
        "fields": [],
    },
    {
        "key": PRESALES_DAILY,
        "name": "Presales — daily",
        "description": (
            "End of day for presales. The tasks section pulls the bids from the "
            "Proposals list; these few extra figures are the ones that are not "
            "derivable from it. Kept short deliberately — a daily report that "
            "takes twenty minutes stops being filed."
        ),
        "fields": PRESALES_DAILY_FIELDS,
    },
    {
        "key": PRESALES_WEEKLY,
        "name": "Presales — weekly",
        "description": (
            "The week for presales: the same figures over seven days, plus what "
            "was won and lost and why, and what the pipeline looks like. The "
            "judgements a single day is too short to support."
        ),
        "fields": PRESALES_WEEKLY_FIELDS,
    },
)

TEMPLATES_BY_KEY: Final[dict[str, dict[str, Any]]] = {t["key"]: t for t in TEMPLATES}
