"""The brief, decided without a database, a key or a network.

The half of this feature worth testing hardest is what the model is *shown*.
Everything downstream of that — whether the paragraph is any good — is the
model's job and cannot be asserted; whether the paragraph describes the right
report, includes every task rather than the first eight, and is not silently
re-used after the report changed, is this file's job and can be.

``render`` and ``fingerprint`` are pure, ``Briefer`` takes its client as a
constructor argument, and ``brief_state`` is a function over two loaded rows.
So none of this needs Postgres, which matters: the route tests against a remote
database take minutes, and these take milliseconds.
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.models.report import (
    BriefFollowup,
    BriefMode,
    IssueSeverity,
    Report,
    ReportCadence,
    ReportIssue,
    ReportMetric,
    ReportSettings,
    ReportStatus,
    ReportTaskLine,
    TaskSource,
)
from app.reports import brief as brief_module
from app.reports import service
from app.reports.brief import Brief, BriefError, Briefer, fingerprint, render

TEAM = uuid.uuid4()
AUTHOR = uuid.uuid4()


def _report(**kw) -> Report:
    row = Report(
        team_id=kw.get("team_id", TEAM),
        author_id=kw.get("author_id", AUTHOR),
        template_id=kw.get("template_id", uuid.uuid4()),
        cadence=kw.get("cadence", ReportCadence.WEEKLY),
        period_start=kw.get("period_start", date(2026, 9, 1)),
        period_end=kw.get("period_end", date(2026, 9, 7)),
    )
    row.status = kw.get("status", ReportStatus.SUBMITTED)
    row.scope = kw.get("scope", "team")
    row.overview = kw.get("overview")
    row.remarks = kw.get("remarks")
    row.summary = kw.get("summary")
    row.answers = kw.get("answers", {})
    # Relationships, assigned rather than loaded — these rows never see a
    # session, so nothing here may trigger a lazy load.
    row.team = SimpleNamespace(name=kw.get("team_name", "Presales"))
    row.author = SimpleNamespace(display_name=kw.get("author_name", "Rameesa"))
    row.template = SimpleNamespace(fields=kw.get("fields", []))
    row.tasks = kw.get("tasks", [])
    row.issues = kw.get("issues", [])
    row.metrics = kw.get("metrics", [])
    row.project_lines = kw.get("project_lines", [])
    row.brief = kw.get("brief")
    row.brief_error = kw.get("brief_error")
    row.brief_input_hash = kw.get("brief_input_hash")
    row.brief_revision = kw.get("brief_revision", 0)
    return row


def _task(title: str, **kw) -> ReportTaskLine:
    row = ReportTaskLine(
        position=kw.get("position", 0),
        source=kw.get("source", TaskSource.MANUAL),
        title=title,
        completion=kw.get("completion", "in_progress"),
    )
    row.percent_complete = kw.get("percent_complete")
    row.status = kw.get("status")
    row.priority = kw.get("priority")
    row.end_user = kw.get("end_user")
    row.quote_no = kw.get("quote_no")
    row.deadline = kw.get("deadline")
    row.note = kw.get("note")
    return row


def _settings(**kw) -> ReportSettings:
    row = ReportSettings(id=1)
    row.brief_enabled = kw.get("brief_enabled", True)
    row.brief_mode = kw.get("brief_mode", BriefMode.ON_SUBMIT)
    row.brief_followup = kw.get("brief_followup", BriefFollowup.CHAT)
    row.brief_model_key = kw.get("brief_model_key")
    row.brief_max_words = kw.get("brief_max_words", 180)
    return row


class _FakeLLM:
    """Stands in for OpenAI. Records what it was asked and answers with a fixture."""

    def __init__(self, reply: object = None, *, configured: bool = True) -> None:
        self._reply = reply if reply is not None else {
            "headline": "Two quotes stuck on Zoho approval",
            "brief": "- Both quotes are waiting on the finance approval.",
        }
        self.configured = configured
        self.calls: list[dict] = []

    async def answer(self, **kwargs):
        self.calls.append(kwargs)
        body = self._reply if isinstance(self._reply, str) else json.dumps(self._reply)
        return body, 1200, 90


# ── what the model is shown ────────────────────────────────────────────


def test_render_names_the_report_before_anything_else() -> None:
    """A manager's first question is whose report this is and for when."""
    text = render(_report(overview="A quiet week."))

    assert "Team: Presales" in text
    assert "Author: Rameesa" in text
    assert "Period: 2026-09-01 to 2026-09-07" in text
    assert "A quiet week." in text


def test_a_daily_report_renders_one_date_not_a_range() -> None:
    text = render(
        _report(
            cadence=ReportCadence.DAILY,
            period_start=date(2026, 9, 8),
            period_end=date(2026, 9, 8),
        )
    )
    assert "Period: 2026-09-08" in text
    assert "2026-09-08 to" not in text


def test_every_task_is_rendered_not_a_sample() -> None:
    """The email truncates its task list; the brief must not.

    A brief written from the first eight of twelve tasks would be confidently
    wrong about the other four, and the whole point is that a manager can trust
    it enough not to open the report.
    """
    tasks = [_task(f"Quote {n}", position=n) for n in range(12)]
    text = render(_report(tasks=tasks))

    for n in range(12):
        assert f"Quote {n}" in text


def test_a_task_carries_what_a_manager_would_ask_about() -> None:
    text = render(
        _report(
            tasks=[
                _task(
                    "ADNOC pump skid",
                    completion="blocked",
                    percent_complete=40,
                    priority="high",
                    end_user="ADNOC",
                    quote_no="Q-1187",
                    deadline=date(2026, 9, 10),
                    note="Waiting on the supplier's revised price.",
                )
            ]
        )
    )

    assert "ADNOC pump skid" in text
    assert "completion: blocked" in text
    assert "40%" in text
    assert "customer: ADNOC" in text
    assert "quote: Q-1187" in text
    assert "deadline: 2026-09-10" in text
    assert "Waiting on the supplier's revised price." in text


def test_an_empty_section_says_so_rather_than_vanishing() -> None:
    """The model cannot tell a skipped section from an empty one; we can."""
    text = render(_report(overview=None, tasks=[], issues=[]))

    assert "OVERVIEW" in text
    assert "ISSUES AND BLOCKERS" in text
    assert text.count("(nothing was entered here)") >= 2


def test_issues_carry_severity_and_who_they_wait_on() -> None:
    issue = ReportIssue(
        position=0,
        title="Zoho approval queue",
        severity=IssueSeverity.BLOCKED,
    )
    issue.detail = "Nothing has moved for four days."
    issue.waiting_on = "Finance"
    issue.resolved = False
    text = render(_report(issues=[issue]))

    assert "Zoho approval queue" in text
    assert "severity: blocked" in text
    assert "waiting on: Finance" in text
    assert "open" in text


def test_a_metric_is_shown_against_its_target() -> None:
    metric = ReportMetric(position=0, key="quotes_sent", label="Quotes sent")
    metric.unit = None
    metric.value = Decimal("7")
    metric.computed = Decimal("6")
    metric.target = Decimal("10")
    text = render(_report(metrics=[metric]))

    assert "Quotes sent: 7" in text
    assert "target: 10" in text
    # The typed figure and the system's disagree, and the brief should be able
    # to say so rather than quietly picking one.
    assert "system figure: 6" in text


def test_the_team_s_own_questions_are_labelled_as_they_were_asked() -> None:
    """Off the report's own template, so a renamed field does not rewrite history."""
    text = render(
        _report(
            answers={"walk_ins": "3"},
            fields=[{"key": "walk_ins", "label": "Walk-in enquiries"}],
        )
    )
    assert "Walk-in enquiries: 3" in text


def test_the_author_s_own_summary_is_kept_separate_from_ours() -> None:
    """``summary`` on a report is the author's section, not the brief."""
    text = render(_report(summary="I think the Zoho issue clears on Monday."))
    assert "THE AUTHOR'S OWN SUMMARY" in text
    assert "I think the Zoho issue clears on Monday." in text


# ── knowing when a brief has gone stale ────────────────────────────────


def test_the_same_report_fingerprints_the_same_twice() -> None:
    report = _report(overview="Same words.")
    assert fingerprint(render(report)) == fingerprint(render(report))


def test_changing_the_report_changes_the_fingerprint() -> None:
    before = fingerprint(render(_report(overview="A quiet week.")))
    after = fingerprint(render(_report(overview="A quiet week, except ADNOC.")))
    assert before != after


def test_adding_a_task_changes_the_fingerprint() -> None:
    before = fingerprint(render(_report(tasks=[_task("One")])))
    after = fingerprint(render(_report(tasks=[_task("One"), _task("Two", position=1)])))
    assert before != after


# ── writing one ────────────────────────────────────────────────────────


async def test_write_returns_the_headline_and_body_it_was_given() -> None:
    briefer = Briefer(_FakeLLM())  # type: ignore[arg-type]
    report = _report(overview="Two quotes blocked.")

    written = await briefer.write(
        report, model="gpt-5-mini", max_words=120, user_key="u1"
    )

    assert isinstance(written, Brief)
    assert written.headline == "Two quotes stuck on Zoho approval"
    assert "finance approval" in written.body
    assert written.model == "gpt-5-mini"
    assert (written.tokens_in, written.tokens_out) == (1200, 90)


async def test_the_fingerprint_describes_what_was_actually_sent() -> None:
    """Not what the report looks like now — what the model was shown.

    The two differ the moment a caller renders once to decide whether a brief
    is needed and the row changes underneath. The stored hash has to match the
    text that produced the brief or it cannot detect anything.
    """
    llm = _FakeLLM()
    briefer = Briefer(llm)  # type: ignore[arg-type]
    report = _report(overview="Now.")

    written = await briefer.write(
        report, model="gpt-5-mini", max_words=120, user_key="u1", source="frozen source"
    )

    assert written.fingerprint == fingerprint("frozen source")
    assert "frozen source" in llm.calls[0]["prompt"]


async def test_the_word_limit_reaches_the_model() -> None:
    llm = _FakeLLM()
    briefer = Briefer(llm)  # type: ignore[arg-type]

    await briefer.write(_report(), model="gpt-5-mini", max_words=90, user_key="u1")

    assert "90 words" in llm.calls[0]["prompt"]


async def test_the_model_is_shown_this_report_and_no_other() -> None:
    """The one property that makes a brief trustworthy.

    If the prompt could carry last week's figures, a manager could not tell
    which sentence came from the person who filed this report.
    """
    llm = _FakeLLM()
    briefer = Briefer(llm)  # type: ignore[arg-type]
    report = _report(overview="This week only.")

    await briefer.write(report, model="gpt-5-mini", max_words=120, user_key="u1")

    prompt = llm.calls[0]["prompt"]
    assert prompt.endswith(render(report))
    assert llm.calls[0]["instructions"] is brief_module.INSTRUCTIONS


async def test_prose_from_a_model_that_ignored_the_schema_is_still_a_brief() -> None:
    """Cosmetic failure, not a missing feature."""
    briefer = Briefer(_FakeLLM("Presales had a quiet week."))  # type: ignore[arg-type]

    written = await briefer.write(_report(), model="gpt-5-mini", max_words=120, user_key="u")

    assert written.headline == ""
    assert written.body == "Presales had a quiet week."


async def test_an_empty_answer_is_refused_rather_than_stored() -> None:
    briefer = Briefer(_FakeLLM({"headline": "x", "brief": "   "}))  # type: ignore[arg-type]

    with pytest.raises(BriefError):
        await briefer.write(_report(), model="gpt-5-mini", max_words=120, user_key="u")


# ── what the page is told ──────────────────────────────────────────────


def test_off_is_reported_as_off_not_as_missing() -> None:
    state = service.brief_state(_report(), _settings(brief_enabled=False))
    assert state == "disabled"


def test_a_draft_is_never_briefed() -> None:
    """A draft changes by the minute and no manager may read it."""
    report = _report(status=ReportStatus.DRAFT)
    assert service.brief_state(report, _settings()) == "not_applicable"


def test_a_filed_report_with_no_brief_yet_is_absent() -> None:
    assert service.brief_state(_report(), _settings()) == "absent"


def test_a_failed_attempt_is_distinguished_from_never_having_tried() -> None:
    report = _report(brief_error="OpenAI is rate limiting this key.")
    assert service.brief_state(report, _settings()) == "failed"


def test_a_brief_written_from_this_report_is_ready() -> None:
    report = _report(overview="A quiet week.")
    report.brief = "Presales had a quiet week."
    report.brief_input_hash = fingerprint(render(report))

    assert service.brief_state(report, _settings()) == "ready"


def test_a_brief_whose_report_has_since_changed_is_stale() -> None:
    report = _report(overview="A quiet week.")
    report.brief = "Presales had a quiet week."
    report.brief_input_hash = fingerprint(render(report))
    report.overview = "Actually, ADNOC cancelled."

    assert service.brief_state(report, _settings()) == "stale"


# ── which model writes them ────────────────────────────────────────────


def test_the_reports_setting_wins_when_it_is_set() -> None:
    assert service.brief_model_key(_settings(brief_model_key="gpt-5-mini"), "gpt-5") == (
        "gpt-5-mini"
    )


def test_otherwise_the_briefs_follow_the_assistant() -> None:
    """So moving the assistant to a cheaper model moves the briefs with it."""
    assert service.brief_model_key(_settings(brief_model_key=None), "gpt-5") == "gpt-5"
