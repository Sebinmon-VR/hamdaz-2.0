"""The shapes a report can have, and the figures that fill themselves in.

Two things live here and nothing else: the sections a report is made of, and
the metrics that can be worked out from its own rows rather than typed.

**Why the sections are code.** A team's questions are data — a super admin adds
"quotes sent" to the presales weekly without a deploy. The *sections* are not,
because their whole value is being the same everywhere. A manager reading four
teams' reports on a Monday morning should find the issues in the same place
each time; a CEO asking "what is blocking us" should get an answer that spans
teams. Both stop being possible the moment a team can rename its issues section
to "challenges" and another can drop it.

So: the frame is fixed, what hangs in it is not.

**Two frames, not one.** The module began with a single frame — the six
sections of a person's account of their own week — and project reporting needs
a second: health dials, a milestone timeline and a portfolio table, none of
which mean anything on a presales daily. The answer is *not* nine sections
everybody has, six of which are empty for most teams. Each template declares
which sections it carries, from the fixed set below, and ``sections_for``
answers what any given template is made of. A template that declares nothing
gets the standard six, so every report written before this existed still means
exactly what it meant.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Callable, Final, Iterable

from app.core.periods import window_for, window_label
from app.models.report import CADENCE_GRAIN
from app.models.templates import FieldType

# ── the six sections ───────────────────────────────────────────────────

OVERVIEW: Final = "overview"
TASKS: Final = "tasks"
ISSUES: Final = "issues"
REMARKS: Final = "remarks"
METRICS: Final = "metrics"
SUMMARY: Final = "summary"

#: The three sections a project status report adds. They are *not* part of the
#: standard six — a presales daily has no milestones and asking it for some
#: would be noise. Which sections a template actually has is declared on the
#: template itself; see ``sections_for`` at the bottom of this file.
HEALTH: Final = "health"
PROJECTS: Final = "projects"
MILESTONES: Final = "milestones"


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

SECTIONS: Final[tuple[Section, ...]] = SECTIONS + (
    Section(
        HEALTH,
        "Health",
        "The five dials — overall, scope, costs, schedule, benefits — each with "
        "which way it is moving, and the percentage complete beside them. Taken "
        "from the project as its lead last assessed it rather than typed here, "
        "so the report and the board cannot disagree about how a project is "
        "doing. Beside each is what the dates or the budget would suggest, "
        "which is worth reading where the two differ.",
        "dials",
    ),
    Section(
        PROJECTS,
        "Projects",
        "One row per project, with its status, health, percentage and the "
        "counts behind it. This is what makes a report cover a whole portfolio "
        "rather than one project — a single-project report has exactly one of "
        "these rows and usually shows it as a header instead of a table.",
        "projects",
    ),
    Section(
        MILESTONES,
        "Milestones",
        "The plan on a timeline: each milestone with its dates, its owner, how "
        "far along it is, and whether it still sits where the plan first put "
        "it. Carries the original date as well as the current one, because the "
        "gap between them is the most useful thing on the chart.",
        "timeline",
    ),
)

SECTIONS_BY_KEY: Final[dict[str, Section]] = {s.key: s for s in SECTIONS}

#: The sections a template may add its own fields to. All of them: a team may
#: want a select in its issues section as readily as a number in its metrics.
SECTION_KEYS: Final[tuple[str, ...]] = tuple(s.key for s in SECTIONS)


# ── which sections a given report has ──────────────────────────────────

#: A person's account of their own period. What every report was before
#: projects existed, and what presales and purchasing still file.
STANDARD_SECTIONS: Final[tuple[str, ...]] = (
    OVERVIEW, TASKS, ISSUES, REMARKS, METRICS, SUMMARY,
)

#: One project over a period — the reference status report. Note it keeps
#: tasks and issues: a status report whose milestones are all green while three
#: people are blocked is a status report that has hidden the interesting part.
PROJECT_SECTIONS: Final[tuple[str, ...]] = (
    OVERVIEW, HEALTH, MILESTONES, TASKS, ISSUES, METRICS, REMARKS, SUMMARY,
)

#: Every project a team runs. No milestone timeline — six projects' milestones
#: on one chart is a chart nobody reads — and no task list, because a portfolio
#: report that descends to individual tasks has stopped being a portfolio
#: report. Issues stay, because escalations are the whole reason it is read.
PORTFOLIO_SECTIONS: Final[tuple[str, ...]] = (
    OVERVIEW, PROJECTS, ISSUES, METRICS, REMARKS, SUMMARY,
)


def section_specs(keys: Iterable[str] = STANDARD_SECTIONS) -> list[dict[str, str]]:
    """The named sections, in the shape ``FormTemplate.sections`` holds.

    Built rather than written out so a template and the skeleton cannot
    disagree: an edited description reaches every template the next time one is
    seeded, and there is no second copy to remember.
    """
    return [
        {
            "key": SECTIONS_BY_KEY[k].key,
            "name": SECTIONS_BY_KEY[k].name,
            "help": SECTIONS_BY_KEY[k].description,
        }
        for k in keys
        if k in SECTIONS_BY_KEY
    ]


def sections_for(template: Any) -> list[Section]:
    """What one template is actually made of.

    Reads the keys off the template's own stored sections and resolves them
    against the fixed set above. A template that declares none — anything
    seeded before project reporting existed, or one an admin cleared — gets the
    standard six, so no report ever renders as a blank page because a list was
    empty.

    Unknown keys are dropped rather than passed through. The section list drives
    what a frontend draws, and a key with no definition behind it is a heading
    with no description and no idea what kind of control belongs under it.
    """
    declared = [
        s.get("key")
        for s in (getattr(template, "sections", None) or [])
        if isinstance(s, dict)
    ]
    resolved = [SECTIONS_BY_KEY[k] for k in declared if k in SECTIONS_BY_KEY]
    return resolved or [SECTIONS_BY_KEY[k] for k in STANDARD_SECTIONS]


# ── what a period is ───────────────────────────────────────────────────


def period_for(cadence: str, on: date) -> tuple[date, date]:
    """The period a report covers, from the cadence and any day inside it.

    Inclusive at both ends, so a daily report's two dates are the same day.
    Weeks run Monday to Sunday — the ISO week, so that "week 41" means the same
    thing here as it does in every calendar the company already uses.

    An ad-hoc report has no period this can know, so it is given the single day
    and the caller is expected to overwrite both ends.

    The arithmetic itself is in ``app.core.periods``, shared with the projects
    module. This function is the translation from a *cadence* to a calendar
    *grain* and nothing more — quarterly and yearly reports arrived with
    project reporting, and having two modules each maintain their own idea of
    which days are in a quarter is the bug that would follow.
    """
    return window_for(CADENCE_GRAIN.get(cadence, "custom"), on)


def period_label(cadence: str, start: date, end: date) -> str:
    """How a period reads to a person. Used in titles and by the assistant."""
    return window_label(CADENCE_GRAIN.get(cadence, "custom"), start, end)


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


# ── the figures a project report works out for itself ──────────────────

#: Computed from the report's **project lines** rather than its task rows, and
#: so a separate set from ``COMPUTED_METRICS`` above. The two never mix: a
#: portfolio report has project lines and no tasks, a presales daily has tasks
#: and no projects, and a single function that tried to serve both would need
#: to guess which it had been handed.
#:
#: Every one of these is a count over the snapshot the report took, not a live
#: query. A metric that re-read the projects would drift away from the table
#: printed directly above it the moment anybody updated anything — which is the
#: same reason the task metrics count the report's own rows.
PROJECT_METRICS: Final[tuple[ComputedMetric, ...]] = (
    ComputedMetric(
        "projects_total", "Projects covered", None,
        "Every project on this report.",
        _count(lambda line: True),
    ),
    ComputedMetric(
        "projects_red", "Red", None,
        "Projects their lead marked red overall. The first number a manager "
        "reads, and the reason the portfolio section exists.",
        _count(lambda line: line.rag_overall == "red"),
    ),
    ComputedMetric(
        "projects_amber", "Amber", None,
        "Projects at risk but not yet in trouble.",
        _count(lambda line: line.rag_overall == "amber"),
    ),
    ComputedMetric(
        "projects_green", "Green", None,
        "Projects their lead is happy with.",
        _count(lambda line: line.rag_overall == "green"),
    ),
    ComputedMetric(
        "projects_unassessed", "Not assessed", None,
        "Projects whose dials nobody has set. Counted deliberately: an "
        "unassessed project is not a green one, and a portfolio where half the "
        "rows are grey is a portfolio nobody is running.",
        _count(lambda line: line.rag_overall in (None, "grey")),
    ),
    ComputedMetric(
        "milestones_overdue", "Milestones overdue", None,
        "Summed across every project here, as of the day the report was drawn.",
        lambda lines: sum(line.milestones_overdue for line in lines),
    ),
    ComputedMetric(
        "tasks_overdue", "Tasks overdue", None,
        "Open work past its date, across every project here.",
        lambda lines: sum(line.tasks_overdue for line in lines),
    ),
    ComputedMetric(
        "tasks_blocked", "Tasks blocked", None,
        "Somebody is stopped. Read next to the issues section.",
        lambda lines: sum(line.tasks_blocked for line in lines),
    ),
    ComputedMetric(
        "issues_open", "Issues open", None,
        "Still unresolved across every project here.",
        lambda lines: sum(line.issues_open for line in lines),
    ),
    ComputedMetric(
        "tasks_completed_in_period", "Completed this period", None,
        "Work finished inside the window this report covers — not the running "
        "total. The one figure that says whether the period itself went well.",
        lambda lines: sum(line.tasks_completed_in_period for line in lines),
    ),
    ComputedMetric(
        "updates_in_period", "Updates this period", None,
        "How many times anybody recorded movement. A project with none is not "
        "necessarily stalled, but it is a project nobody has written about.",
        lambda lines: sum(line.updates_in_period for line in lines),
    ),
)

PROJECT_METRICS_BY_KEY: Final[dict[str, ComputedMetric]] = {
    m.key: m for m in PROJECT_METRICS
}


def compute_projects(lines: Iterable) -> dict[str, Decimal]:
    """Every project metric, over one report's project lines."""
    rows = list(lines)
    return {metric.key: Decimal(metric.of(rows)) for metric in PROJECT_METRICS}


def average_percent(lines: Iterable) -> int:
    """Mean completion across the projects on a report.

    Unweighted — every project counts once, whatever its size. A weighted
    average would need a measure of size that nothing here has, and inventing
    one from task counts would make a project with lots of small tasks look
    more important than one with a few large ones.
    """
    rows = list(lines)
    if not rows:
        return 0
    return int(round(sum(r.percent_complete for r in rows) / len(rows)))


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

#: The project reporting family. One project in depth, one project briefly, and
#: every project at once — the three shapes the reference layouts describe.
#: A team files these by having a schedule pointed at them; nothing about a
#: template names a team. That is why "select the AI team and the form changes"
#: needed no new mechanism, only new templates.
PROJECT_STATUS: Final = "report_project_status"
PROJECT_EXEC: Final = "report_project_exec"
PORTFOLIO_STATUS: Final = "report_portfolio_status"


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


#: The note every section of every report can carry, and the one word that
#: says what kind of note it is.
#:
#: Two fields rather than one, and the dropdown is the point. A week of free
#: text is unreadable across six people; a week of "supplier risk" beside the
#: free text is a column somebody can sort by, count, and notice getting
#: longer. The prose stays because a tag on its own explains nothing — the tag
#: is for the reader scanning ten reports, the prose for the one who stops.
def _note_fields(section: str, *, label: str = "Note") -> list[dict[str, Any]]:
    return [
        _field(
            "note_kind", "What kind of note", FieldType.SELECT, section=section,
            options=[
                "nothing to flag",
                "customer risk",
                "supplier risk",
                "pricing",
                "lead time",
                "technical",
                "capacity",
                "process",
                "good news",
                "other",
            ],
            help="One word for whoever is reading six of these. Pick the "
                 "closest; 'other' is honest when nothing fits.",
        ),
        _field(
            "note", label, FieldType.TEXTAREA, section=section,
            help="Anything the sections above have no room for. Written for "
                 "somebody who was not in the room.",
        ),
    ]


#: What presales is asked at the end of a day, beyond the six sections.
#:
#: Short on purpose. A daily report that takes twenty minutes is a daily report
#: that gets filed for a fortnight and then stops, and the tasks section already
#: carries the work itself — these are the few things not derivable from it.
#:
#: **Restructured to read like the project reports the AI team files.** It was
#: six number boxes and nothing else, which is a form rather than a report: a
#: manager could see that four quotes went out and nothing about whether the
#: day went well. The headline and the two notes are what turn a list of
#: figures back into an account of a day.
PRESALES_DAILY_FIELDS: Final[list[dict[str, Any]]] = [
    _field(
        "headline", "The day in one line", FieldType.TEXT, section=OVERVIEW,
        required=True,
        help="What a manager should know if they read nothing else. "
             "\"Two quotes out, ADNOC still waiting on Gulf Valves.\"",
    ),
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
        "waiting_on", "Waiting on somebody else", FieldType.TEXTAREA, section=ISSUES,
        help="Who owes you what, by name. The single most useful line in a "
             "daily report and the one most often left out.",
    ),
    _field(
        "support_needed", "Support needed", FieldType.SELECT, section=ISSUES,
        options=["none", "pricing approval", "technical input", "supplier contact",
                 "customer escalation", "other"],
        help="What would move things along fastest. Read by whoever gets this.",
    ),
    *_note_fields(REMARKS),
    _field(
        "tomorrow_focus", "Focus tomorrow", FieldType.TEXTAREA, section=SUMMARY,
        help="The one or two things being picked up first.",
    ),
]

#: The week. Same spine as the daily — a headline, the figures, what is stuck,
#: a note, what is next — with the numbers that only mean anything over seven
#: days: value quoted, won and lost, the live pipeline.
PRESALES_WEEKLY_FIELDS: Final[list[dict[str, Any]]] = [
    _field(
        "headline", "The week in one line", FieldType.TEXT, section=OVERVIEW,
        required=True,
        help="The one sentence somebody reading nine reports will remember.",
    ),
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
        "pipeline_value", "Live pipeline", FieldType.CURRENCY, section=METRICS,
        help="Everything still open at the end of the week, in AED.",
    ),
    _field(
        "closing_next_week", "Closing next week", FieldType.NUMBER, section=METRICS,
        help="Bids whose closing date falls in the coming week.",
    ),
    _field(
        "loss_reasons", "Why they were lost", FieldType.TEXTAREA, section=ISSUES,
        help="Price, lead time, specification, no reason given. The one field "
             "here that changes what the company does next.",
    ),
    _field(
        "waiting_on", "Waiting on somebody else", FieldType.TEXTAREA, section=ISSUES,
        help="Who owes you what, by name, and since when.",
    ),
    _field(
        "support_needed", "Support needed", FieldType.SELECT, section=ISSUES,
        options=["none", "pricing approval", "technical input", "supplier contact",
                 "customer escalation", "more capacity", "other"],
    ),
    *_note_fields(REMARKS),
    _field(
        "next_week_focus", "Focus next week", FieldType.TEXTAREA, section=SUMMARY,
        required=True,
    ),
]


#: What a full project status report asks on top of what it already knows.
#:
#: Short, and everything here is a judgement rather than a figure. The dials,
#: the percentages, the milestone dates, the counts and the issue list all come
#: from the project itself — retyping any of them would be both tedious and a
#: chance for the report to contradict the board. What is left is the part only
#: the lead can supply: what happens next, what they need, and how confident
#: they actually are.
PROJECT_STATUS_FIELDS: Final[list[dict[str, Any]]] = [
    _field(
        "confidence", "Confidence in the target date", FieldType.SELECT,
        section=HEALTH, required=True,
        options=["on track", "at risk", "will slip", "already slipped", "too early to say"],
        help="Your judgement, not the arithmetic. The dates say what they say; "
             "this says whether you believe them.",
    ),
    _field(
        "decisions_needed", "Decisions needed", FieldType.TEXTAREA, section=ISSUES,
        help="What somebody above you has to decide before the next report. "
             "The one field on here that changes what happens next — leave it "
             "empty if there is genuinely nothing.",
    ),
    _field(
        "resource_needs", "Support or people needed", FieldType.TEXTAREA, section=ISSUES,
        help="What would move this along fastest. Read by whoever gets this.",
    ),
    _field(
        "risks_ahead", "Risks in the coming period", FieldType.TEXTAREA, section=REMARKS,
        help="What could go wrong that has not yet. Kept apart from issues on "
             "purpose: an issue is happening, a risk has not started.",
    ),
    _field(
        "scope_changes", "Scope changes this period", FieldType.TEXTAREA, section=HEALTH,
        help="Anything added, dropped or reinterpreted. The reason the scope "
             "dial moved, or the evidence that it should not have.",
    ),
    _field(
        "next_period_focus", "Focus next period", FieldType.TEXTAREA,
        section=SUMMARY, required=True,
        help="The one or two things being picked up first.",
    ),
]

#: The weekly tactical version — the second layout in the reference.
#:
#: Deliberately four questions. A status report filed every week has to be
#: fillable in five minutes or it stops being filed by the third week, and the
#: milestone timeline and issue table underneath it are already carrying most
#: of the content without anybody typing anything.
PROJECT_EXEC_FIELDS: Final[list[dict[str, Any]]] = [
    _field(
        "headline", "Where it stands, in one line", FieldType.TEXT,
        section=OVERVIEW, required=True,
        help="What somebody with ten seconds should take away.",
    ),
    _field(
        "confidence", "Confidence in the target date", FieldType.SELECT,
        section=HEALTH,
        options=["on track", "at risk", "will slip", "already slipped", "too early to say"],
    ),
    _field(
        "support_needed", "Support needed", FieldType.SELECT, section=ISSUES,
        options=["none", "a decision", "more people", "budget", "another team",
                 "external supplier", "other"],
        help="What would move things along fastest. Say 'none' rather than "
             "leaving it — a blank reads as 'not filled in', not as 'nothing'.",
    ),
    _field(
        "next_week_focus", "Focus next week", FieldType.TEXTAREA,
        section=SUMMARY, required=True,
    ),
]

#: Every project a team runs, on one page.
#:
#: The questions are about the set rather than about any project in it. Asking
#: "what is blocking this project" here would be the wrong altitude — that is
#: what the per-project reports are for, and the portfolio row already carries
#: each project's counts.
PORTFOLIO_STATUS_FIELDS: Final[list[dict[str, Any]]] = [
    _field(
        "headline", "The portfolio in one line", FieldType.TEXT,
        section=OVERVIEW, required=True,
    ),
    _field(
        "escalations", "What needs a decision", FieldType.TEXTAREA, section=ISSUES,
        help="Across every project here. The part a manager reads first, and "
             "often the only part they read.",
    ),
    _field(
        "resourcing", "Where people are stretched", FieldType.TEXTAREA, section=REMARKS,
        help="Which projects are short and which have capacity. The question a "
             "portfolio view exists to answer and that no single project can.",
    ),
    _field(
        "starting_next", "Starting next period", FieldType.TEXTAREA, section=SUMMARY,
        help="Work about to begin. Worth saying here because it is the thing "
             "the per-project reports cannot cover — those projects have no "
             "report yet.",
    ),
    _field(
        "closing_next", "Expected to finish next period", FieldType.TEXTAREA,
        section=SUMMARY,
    ),
]


#: The report templates the product ships with. Seeded like the form catalogue:
#: code, not user data, because the reports module refers to the generic one by
#: key. A super admin edits any of it afterwards and the seed will not undo it.
TEMPLATES: Final[tuple[dict[str, Any], ...]] = (
    {
        "key": GENERIC_REPORT,
        "sections": STANDARD_SECTIONS,
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
        "sections": STANDARD_SECTIONS,
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
        "sections": STANDARD_SECTIONS,
        "name": "Presales — weekly",
        "description": (
            "The week for presales: the same figures over seven days, plus what "
            "was won and lost and why, and what the pipeline looks like. The "
            "judgements a single day is too short to support."
        ),
        "fields": PRESALES_WEEKLY_FIELDS,
    },
    {
        "key": PROJECT_STATUS,
        "sections": PROJECT_SECTIONS,
        "scope": "project",
        "name": "Project status report",
        "description": (
            "One project in full: the five health dials, the milestone "
            "timeline with each milestone's owner and percentage, the open "
            "work, the issues, and what the lead needs decided. Everything "
            "countable is pulled from the project itself, so the report and "
            "the board cannot disagree — what is asked here is only the part "
            "a person has to supply. Written for a monthly or quarterly "
            "review."
        ),
        "fields": PROJECT_STATUS_FIELDS,
    },
    {
        "key": PROJECT_EXEC,
        "sections": PROJECT_SECTIONS,
        "scope": "project",
        "name": "Executive project status",
        "description": (
            "The same project, the weekly version: a headline, the dials, the "
            "timeline, what is in the way and what is being picked up next. "
            "Four questions on purpose — a weekly report that takes twenty "
            "minutes is a weekly report that stops being filed by the third "
            "week."
        ),
        "fields": PROJECT_EXEC_FIELDS,
    },
    {
        "key": PORTFOLIO_STATUS,
        "sections": PORTFOLIO_SECTIONS,
        "scope": "portfolio",
        "name": "Portfolio status report",
        "description": (
            "Every project a team runs, one row each, with its status, health, "
            "percentage and the counts behind it — plus what needs deciding "
            "across all of them and where people are stretched. No milestone "
            "timeline and no task list: at this altitude they are noise, and "
            "the per-project reports carry them."
        ),
        "fields": PORTFOLIO_STATUS_FIELDS,
    },
)

TEMPLATES_BY_KEY: Final[dict[str, dict[str, Any]]] = {t["key"]: t for t in TEMPLATES}


def scope_of(template: Any) -> str:
    """What a template is for: ``team``, ``project`` or ``portfolio``.

    **Inferred from the sections the template declares, not stored on it.** A
    template carrying the portfolio table is a portfolio report; one carrying
    health dials or a milestone timeline is about a single project; anything
    else is the personal report the module started with.

    Inferring rather than adding a column is deliberate. A super admin can
    build their own report template through the existing editor, and a scope
    column would be a second thing they had to set correctly — one that could
    contradict the sections they actually chose, leaving a "project" report
    with nowhere to put a project. The sections *are* the scope; asking twice
    would only create the chance of two answers.
    """
    keys = {
        s.get("key")
        for s in (getattr(template, "sections", None) or [])
        if isinstance(s, dict)
    }
    if PROJECTS in keys:
        return "portfolio"
    if HEALTH in keys or MILESTONES in keys:
        return "project"
    return "team"


#: Which shipped template a team gets when it adopts project reporting, per
#: cadence. Used by the one administrative action that wires a team up — see
#: ``app.reports.service.adopt_project_reporting``. A daily project status
#: report is deliberately absent: a project does not change enough in a day to
#: be worth a set of dials, and offering one would get it asked for.
PROJECT_SCHEDULE_DEFAULTS: Final[dict[str, str]] = {
    "weekly": PROJECT_EXEC,
    "monthly": PROJECT_STATUS,
    "quarterly": PROJECT_STATUS,
}
