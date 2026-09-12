"""The manager's brief: one report, said short.

A filled-in report is long on purpose. Six sections, every task with its
status, every issue with its severity, every metric against its target, and
whatever else that team's template asks. That is the right shape for a record
somebody may have to look up in March, and the wrong shape for a manager with
nine of them open on a Monday morning — which is the complaint this module
exists to answer.

**Nothing here reads the database.** ``render`` takes a report that is already
loaded and turns it into text; ``Briefer.write`` takes that text and returns a
brief. Both are pure over their inputs, so the interesting half of this feature
— what the model is actually shown — can be tested without a database, a key,
or a network, and reviewed by reading one function.

**The model is shown the report and nothing else.** No other reports, no team
history, no project state. A brief that quietly draws on facts the report does
not contain is worse than no brief: the manager cannot tell which sentence came
from the person who filed it. Everything the assistant needs beyond this one
report, it fetches through its own tools in the chat that follows, where the
fetching is visible.

**A brief is cached against a fingerprint of its input.** A submitted report
never changes, so its brief is written once and read many times. A draft that
is briefed and then edited has a brief that no longer describes it, and
``fingerprint`` is how that is noticed rather than assumed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from app.assistant.llm import LLMError, OpenAIChat
from app.models.report import Report

#: What the model is asked to produce. Strict, and flat on purpose — a nested
#: object here buys nothing and strict mode makes every level of nesting a
#: place for the schema to be subtly wrong.
SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["headline", "brief"],
    "properties": {
        "headline": {
            "type": "string",
            "description": (
                "One line, under 140 characters, that a manager could read in a "
                "list of reports and know whether to open this one. State what "
                "actually happened or what is stuck. Never 'weekly report'."
            ),
        },
        "brief": {
            "type": "string",
            "description": (
                "The brief itself, as plain text with '- ' bullets where a list "
                "helps. No markdown headings, no bold, no preamble."
            ),
        },
    },
}

INSTRUCTIONS: Final[str] = """\
You write briefs on team reports for the managers who have to read them.

Your reader runs several teams and has a stack of reports to get through. They \
need to know, in the time it takes to read a paragraph: what moved, what is \
stuck, what needs a decision from them, and whether anything in here is worse \
than it looks.

How to write one:
- Lead with the thing that matters most, which is usually a blocker, a slipped \
deadline or a number that moved the wrong way. Not a recap of the period.
- Say what is blocked and who or what it is waiting on, by name, exactly as the \
report does.
- Give figures when the report gives them, with the target beside them if it has \
one. Round nothing and invent nothing.
- Name anything that needs the manager to act, and say what the action is.
- Say plainly when a section is empty or vague: "no issues were listed" is \
useful; filling the space is not.

Hard rules:
- Use only what is in the report below. If it is not there, it does not go in \
the brief. Never guess at a cause, a next step, or how a number compares to \
last week — you have not been shown last week.
- Do not soften and do not dramatise. A quiet week reads as a quiet week.
- Write about the author in the third person, by name.
- No preamble, no sign-off, no "this report shows". Start with the substance.
"""

#: Rendered when a section holds nothing. Said out loud rather than left out,
#: because the model cannot tell an empty section from one that was skipped,
#: and a manager reading "no issues were listed" is better served than one
#: reading a brief that silently omits the issues section.
_EMPTY: Final[str] = "(nothing was entered here)"

#: How much of one free-text answer is shown. Long enough for a paragraph
#: somebody actually wrote, short enough that one essay cannot crowd out the
#: rest of the report.
_MAX_TEXT: Final[int] = 4000


class BriefError(Exception):
    """A brief could not be written. The message is safe to show a person."""


@dataclass(frozen=True, slots=True)
class Brief:
    """One written brief, and what it cost."""

    headline: str
    body: str
    model: str
    fingerprint: str
    tokens_in: int
    tokens_out: int


def _text(value: str | None) -> str:
    value = (value or "").strip()
    if not value:
        return _EMPTY
    return value[:_MAX_TEXT] + ("…" if len(value) > _MAX_TEXT else "")


def _number(value: Decimal | int | float | None) -> str:
    """A figure as somebody would write it.

    ``Decimal.normalize`` alone is a trap here: it turns ``10`` into ``1E+1``,
    and a target rendered as ``1E+1`` is a target the model has to interpret
    rather than read. Whole numbers are quantised, the rest are normalised to
    drop trailing zeros, and both are then formatted positionally.
    """
    if value is None:
        return "—"
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return f"{value.quantize(Decimal(1)):f}"
        return f"{value.normalize():f}"
    return f"{value:g}"


def _labels(report: Report) -> dict[str, str]:
    """Template field keys to the question as it was asked.

    Off the report's own template, so a report filed against version 3 is
    described in version 3's words even after somebody edits the template.
    """
    template = report.template
    fields = getattr(template, "fields", None) or []
    return {
        f["key"]: (f.get("label") or f["key"])
        for f in fields
        if isinstance(f, dict) and f.get("key")
    }


def _period(report: Report) -> str:
    if report.period_start == report.period_end:
        return report.period_start.isoformat()
    return f"{report.period_start.isoformat()} to {report.period_end.isoformat()}"


def render(report: Report) -> str:
    """The report as the model sees it.

    Deterministic, ordered and labelled — the same report renders to the same
    string every time, which is what makes ``fingerprint`` able to tell "this
    report changed" from "this report was read again".

    Rows are rendered in full rather than sampled. A brief written from the
    first eight tasks would be confidently wrong about the ninth, and the whole
    point of the thing is that the manager can trust it enough not to open the
    report.
    """
    lines: list[str] = [
        "REPORT",
        f"Team: {report.team.name}",
        f"Author: {report.author.display_name}",
        f"Cadence: {report.cadence}",
        f"Period: {_period(report)}",
        f"Scope: {report.scope}",
        f"Status: {report.status}",
    ]
    if report.project_lines and report.scope != "team":
        names = ", ".join(line.name for line in report.project_lines)
        lines.append(f"Projects covered: {names}")

    lines += ["", "OVERVIEW", _text(report.overview)]

    lines += ["", "TASKS"]
    if not report.tasks:
        lines.append(_EMPTY)
    for task in report.tasks:
        bits = [f"- {task.title}", f"completion: {task.completion}"]
        if task.percent_complete is not None:
            bits.append(f"{task.percent_complete}%")
        if task.status:
            bits.append(f"status: {task.status}")
        if task.priority:
            bits.append(f"priority: {task.priority}")
        if task.end_user:
            bits.append(f"customer: {task.end_user}")
        if task.quote_no:
            bits.append(f"quote: {task.quote_no}")
        if task.deadline:
            bits.append(f"deadline: {task.deadline.isoformat()}")
        line = " | ".join(bits)
        if task.note:
            line += f"\n    note: {task.note.strip()[:600]}"
        lines.append(line)

    lines += ["", "ISSUES AND BLOCKERS"]
    if not report.issues:
        lines.append(_EMPTY)
    for issue in report.issues:
        bits = [f"- {issue.title}", f"severity: {issue.severity}"]
        if issue.waiting_on:
            bits.append(f"waiting on: {issue.waiting_on}")
        bits.append("resolved" if issue.resolved else "open")
        line = " | ".join(bits)
        if issue.detail:
            line += f"\n    {issue.detail.strip()[:600]}"
        lines.append(line)

    lines += ["", "METRICS"]
    if not report.metrics:
        lines.append(_EMPTY)
    for metric in report.metrics:
        value = metric.value if metric.value is not None else metric.computed
        bits = [f"- {metric.label}: {_number(value)}"]
        if metric.unit:
            bits.append(f"unit: {metric.unit}")
        if metric.target is not None:
            bits.append(f"target: {_number(metric.target)}")
        if metric.computed is not None and metric.value is not None:
            bits.append(f"(system figure: {_number(metric.computed)})")
        lines.append(" | ".join(bits))

    for line in report.project_lines:
        lines += ["", f"PROJECT: {line.name}" + (f" ({line.code})" if line.code else "")]
        lines.append(
            f"Health — overall {line.rag_overall}, scope {line.rag_scope}, "
            f"cost {line.rag_cost}, schedule {line.rag_schedule}, "
            f"benefits {line.rag_benefits}"
        )
        lines.append(
            f"{line.percent_complete}% complete | tasks {line.tasks_done}/{line.tasks_total} "
            f"done, {line.tasks_blocked} blocked, {line.tasks_overdue} overdue | "
            f"milestones {line.milestones_done}/{line.milestones_total} done, "
            f"{line.milestones_overdue} overdue | {line.issues_open} open issues"
        )
        if line.activities:
            lines.append(f"Activities: {line.activities.strip()[:1200]}")
        if line.action_required:
            lines.append(f"Action required: {line.action_required.strip()[:1200]}")
        for milestone in getattr(line, "milestones", []) or []:
            due = milestone.due_on.isoformat() if milestone.due_on else "no date"
            lines.append(
                f"  - {milestone.name} | due {due} | {milestone.percent_complete}% | "
                f"{milestone.plan}"
            )

    labels = _labels(report)
    answers = report.answers or {}
    if answers:
        lines += ["", "THIS TEAM'S OWN QUESTIONS"]
        for key, value in answers.items():
            if value in (None, "", [], {}):
                continue
            lines.append(f"- {labels.get(key, key)}: {_text(str(value))}")

    lines += ["", "REMARKS", _text(report.remarks)]
    lines += ["", "THE AUTHOR'S OWN SUMMARY", _text(report.summary)]
    return "\n".join(lines)


def fingerprint(source: str) -> str:
    """What the brief was written from, as a hash.

    Compared against the stored one to answer "does this brief still describe
    this report". Over the rendered text rather than the row's ``updated_at``
    because a report can be saved without changing anything a reader would see,
    and re-writing a brief for that is money spent on an identical answer.
    """
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


class Briefer:
    """Writes briefs. Holds no state beyond the shared OpenAI client."""

    def __init__(self, llm: OpenAIChat) -> None:
        self._llm = llm

    @property
    def configured(self) -> bool:
        return self._llm.configured

    async def write(
        self,
        report: Report,
        *,
        model: str,
        max_words: int,
        user_key: str,
        source: str | None = None,
    ) -> Brief:
        """One call, one brief.

        ``source`` may be passed in by a caller that has already rendered the
        report to decide whether a brief was needed at all — rendering it twice
        would be harmless but would also let the two copies disagree, and the
        fingerprint must describe the text that was actually sent.
        """
        source = render(report) if source is None else source
        prompt = (
            f"Write the brief in at most {max_words} words.\n\n"
            f"{source}"
        )
        try:
            raw, tokens_in, tokens_out = await self._llm.answer(
                model=model,
                instructions=INSTRUCTIONS,
                prompt=prompt,
                schema=SCHEMA,
                reasoning_effort="low",
                max_output_tokens=max(1200, max_words * 8),
                user_key=user_key,
            )
        except LLMError as exc:
            raise BriefError(str(exc)) from exc

        headline, body = _parse(raw)
        if not body:
            raise BriefError("The model returned an empty brief.")
        return Brief(
            headline=headline,
            body=body,
            model=model,
            fingerprint=fingerprint(source),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )


def _parse(raw: str) -> tuple[str, str]:
    """Pull the headline and body out of the model's JSON.

    Falls back to treating the whole thing as the body rather than failing. A
    brief that arrived as prose because a model ignored the schema is still a
    brief; refusing it would turn a cosmetic problem into a missing feature.
    """
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return "", raw.strip()
    if not isinstance(payload, dict):
        return "", raw.strip()
    headline = str(payload.get("headline") or "").strip()[:300]
    body = str(payload.get("brief") or "").strip()
    return headline, body
