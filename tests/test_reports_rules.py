"""The reporting rules, decided without a database.

Who may read whose report is the half of this module worth testing hardest: a
report names customers, prices and what somebody is stuck on, and the person
who wrote it did so expecting their manager to read it and not the company. All
of that is pure functions over loaded rows, so every case here runs in
milliseconds and is checked the same way however the request arrived — the
page, the API, or the assistant asking on somebody's behalf.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.models.report import (
    IssueSeverity,
    Report,
    ReportCadence,
    ReportStatus,
    ReportTaskLine,
    TaskSource,
)
from app.reports.access import (
    COMPANY_WIDE,
    TEAM_OVERSIGHT,
    Viewer,
    may_comment,
    may_delete,
    may_edit,
    may_file_for,
    may_read,
    readable_team_ids,
)
from app.reports.catalogue import (
    COMPLETION_LABELS,
    COMPLETIONS,
    COMPUTED_METRICS,
    SECTIONS,
    SECTIONS_BY_KEY,
    TEMPLATES,
    compute,
    period_for,
    period_label,
    section_specs,
)
from app.reports.service import _clean_emails, merge_answers, validate_answers

TEAM = uuid.uuid4()
OTHER_TEAM = uuid.uuid4()
AUTHOR = uuid.uuid4()


def _report(**kw) -> Report:
    row = Report(
        team_id=kw.get("team_id", TEAM),
        author_id=kw.get("author_id", AUTHOR),
        template_id=kw.get("template_id", uuid.uuid4()),
        cadence=kw.get("cadence", ReportCadence.DAILY),
        period_start=kw.get("period_start", date(2026, 9, 8)),
        period_end=kw.get("period_end", date(2026, 9, 8)),
    )
    row.status = kw.get("status", ReportStatus.SUBMITTED)
    return row


def _viewer(**kw) -> Viewer:
    return Viewer(
        user_id=kw.get("user_id", uuid.uuid4()),
        roles=frozenset(kw.get("roles", ())),
        team_ids=frozenset(kw.get("team_ids", ())),
        oversees=frozenset(kw.get("oversees", ())),
    )


# ── who may read one ───────────────────────────────────────────────────


def test_an_author_reads_their_own() -> None:
    assert may_read(_report(), _viewer(user_id=AUTHOR)) is True


def test_an_author_reads_their_own_draft() -> None:
    report = _report(status=ReportStatus.DRAFT)
    assert may_read(report, _viewer(user_id=AUTHOR)) is True


def test_a_colleague_on_the_same_team_reads_nothing() -> None:
    """Being on presales does not make a colleague's report yours to read.
    This is the case the whole module exists to get right."""
    colleague = _viewer(team_ids={TEAM})
    assert may_read(_report(), colleague) is False


def test_a_team_lead_reads_their_teams_submitted_reports() -> None:
    lead = _viewer(team_ids={TEAM}, oversees={TEAM})
    assert may_read(_report(), lead) is True


def test_a_team_lead_of_another_team_reads_nothing() -> None:
    """Oversight is held per team, so holding it says nothing about any other."""
    lead = _viewer(team_ids={OTHER_TEAM}, oversees={OTHER_TEAM})
    assert may_read(_report(), lead) is False


@pytest.mark.parametrize("role", sorted(COMPANY_WIDE))
def test_the_company_wide_roles_read_every_team(role: str) -> None:
    assert may_read(_report(), _viewer(roles={role})) is True


def test_an_accountant_is_not_one_of_them() -> None:
    """Reading the accounts is a different question from reading what presales
    were blocked on last Tuesday."""
    assert may_read(_report(), _viewer(roles={"accountant"})) is False


def test_a_draft_is_its_authors_alone_even_from_a_ceo() -> None:
    """Somebody's half-written notes read as a finished report is how people
    learn to draft somewhere else and paste it in at the end."""
    draft = _report(status=ReportStatus.DRAFT)
    assert may_read(draft, _viewer(roles={"ceo"})) is False
    assert may_read(draft, _viewer(roles={"super_admin"})) is False
    assert may_read(draft, _viewer(oversees={TEAM})) is False


# ── who may change one ─────────────────────────────────────────────────


def test_only_the_author_edits_and_only_while_it_is_a_draft() -> None:
    draft = _report(status=ReportStatus.DRAFT)
    assert may_edit(draft, _viewer(user_id=AUTHOR)) is True
    assert may_edit(_report(), _viewer(user_id=AUTHOR)) is False


def test_a_super_admin_cannot_edit_somebody_elses_report() -> None:
    """Editing somebody's account of their own week is not an administrative
    act — the record would still carry their name."""
    draft = _report(status=ReportStatus.DRAFT)
    assert may_edit(draft, _viewer(roles={"super_admin"})) is False


def test_a_super_admin_can_delete_one_and_a_ceo_cannot() -> None:
    """Reading everything and being able to remove it are different powers."""
    assert may_delete(_report(), _viewer(roles={"super_admin"})) is True
    assert may_delete(_report(), _viewer(roles={"ceo"})) is False


def test_an_author_deletes_their_draft_but_not_their_filed_report() -> None:
    assert may_delete(_report(status=ReportStatus.DRAFT), _viewer(user_id=AUTHOR)) is True
    assert may_delete(_report(), _viewer(user_id=AUTHOR)) is False


# ── who may comment ────────────────────────────────────────────────────


def test_a_reader_may_comment_on_a_submitted_report() -> None:
    assert may_comment(_report(), _viewer(oversees={TEAM})) is True


def test_an_author_may_not_comment_on_their_own() -> None:
    """They have the remarks section. Letting them append after filing would
    make "what did they report on Tuesday" unanswerable."""
    assert may_comment(_report(), _viewer(user_id=AUTHOR)) is False


def test_nobody_comments_on_a_draft() -> None:
    draft = _report(status=ReportStatus.DRAFT)
    assert may_comment(draft, _viewer(roles={"ceo"})) is False


def test_somebody_who_cannot_read_it_cannot_comment() -> None:
    assert may_comment(_report(), _viewer(team_ids={TEAM})) is False


# ── who may file one ───────────────────────────────────────────────────


def test_filing_needs_membership_not_oversight() -> None:
    """Filing a report is what an ordinary member does."""
    assert may_file_for(TEAM, _viewer(team_ids={TEAM})) is True
    assert may_file_for(TEAM, _viewer(team_ids={OTHER_TEAM})) is False


def test_a_super_admin_may_file_anywhere() -> None:
    assert may_file_for(TEAM, _viewer(roles={"super_admin"})) is True


def test_a_ceo_who_is_on_no_team_cannot_file_for_one() -> None:
    """Reading every report does not make you a member of every team."""
    assert may_file_for(TEAM, _viewer(roles={"ceo"})) is False


# ── narrowing a query ──────────────────────────────────────────────────


def test_company_wide_readers_get_no_team_filter_at_all() -> None:
    """None rather than "every id", so a team created tomorrow does not quietly
    become invisible to the CEO because a set was built today."""
    assert readable_team_ids(_viewer(roles={"ceo"})) is None


def test_everybody_else_gets_the_teams_they_run() -> None:
    assert readable_team_ids(_viewer(oversees={TEAM})) == frozenset({TEAM})
    assert readable_team_ids(_viewer(team_ids={TEAM})) == frozenset()


def test_the_oversight_roles_are_the_team_scoped_ones() -> None:
    assert TEAM_OVERSIGHT == {"team_manager", "team_lead"}


# ── what a period is ───────────────────────────────────────────────────


def test_a_daily_period_is_one_day_twice() -> None:
    """Both ends inclusive, so every query over a range is written one way."""
    assert period_for(ReportCadence.DAILY, date(2026, 9, 8)) == (
        date(2026, 9, 8), date(2026, 9, 8)
    )


def test_a_week_runs_monday_to_sunday() -> None:
    # 2026-09-08 is a Tuesday.
    assert period_for(ReportCadence.WEEKLY, date(2026, 9, 8)) == (
        date(2026, 9, 7), date(2026, 9, 13)
    )


def test_any_day_in_the_week_gives_the_same_week() -> None:
    monday = period_for(ReportCadence.WEEKLY, date(2026, 9, 7))
    sunday = period_for(ReportCadence.WEEKLY, date(2026, 9, 13))
    assert monday == sunday


@pytest.mark.parametrize(
    "day,expected",
    [
        (date(2026, 9, 8), (date(2026, 9, 1), date(2026, 9, 30))),
        (date(2026, 2, 14), (date(2026, 2, 1), date(2026, 2, 28))),
        (date(2028, 2, 14), (date(2028, 2, 1), date(2028, 2, 29))),  # a leap year
        (date(2026, 12, 31), (date(2026, 12, 1), date(2026, 12, 31))),
    ],
)
def test_a_month_is_the_calendar_month(day: date, expected: tuple) -> None:
    assert period_for(ReportCadence.MONTHLY, day) == expected


def test_a_period_reads_as_a_person_would_say_it() -> None:
    assert "Tuesday" in period_label(ReportCadence.DAILY, date(2026, 9, 8), date(2026, 9, 8))
    assert period_label(
        ReportCadence.MONTHLY, date(2026, 9, 1), date(2026, 9, 30)
    ) == "September 2026"


# ── the figures that fill themselves in ────────────────────────────────


def _line(**kw) -> ReportTaskLine:
    row = ReportTaskLine(
        title=kw.get("title", "A bid"),
        position=0,
        source=kw.get("source", TaskSource.MANUAL),
    )
    row.completion = kw.get("completion", "in_progress")
    row.deadline = kw.get("deadline")
    row.has_attachments = kw.get("has_attachments", False)
    return row


def test_the_counts_add_up() -> None:
    lines = [
        _line(completion="done"),
        _line(completion="done"),
        _line(completion="in_progress"),
        _line(completion="blocked"),
        _line(completion="not_started"),
    ]
    figures = compute(lines)
    assert figures["tasks_total"] == 5
    assert figures["tasks_done"] == 2
    assert figures["tasks_in_progress"] == 1
    assert figures["tasks_blocked"] == 1


def test_overdue_is_judged_on_the_deadline_not_the_status() -> None:
    """A bid whose closing date has passed and which is not marked done is late
    whatever the source list says about it."""
    lines = [
        _line(completion="in_progress", deadline=date(2020, 1, 1)),
        _line(completion="done", deadline=date(2020, 1, 1)),
        _line(completion="in_progress", deadline=date(2099, 1, 1)),
        _line(completion="in_progress", deadline=None),
    ]
    assert compute(lines)["tasks_overdue"] == 1


def test_an_empty_report_computes_zeroes_rather_than_nothing() -> None:
    figures = compute([])
    assert set(figures) == {m.key for m in COMPUTED_METRICS}
    assert all(v == Decimal(0) for v in figures.values())


# ── the skeleton ───────────────────────────────────────────────────────


def test_the_six_sections_are_the_ones_asked_for() -> None:
    assert [s.key for s in SECTIONS] == [
        "overview", "tasks", "issues", "remarks", "metrics", "summary"
    ]


def test_every_section_says_how_to_render_it() -> None:
    for section in SECTIONS:
        assert section.kind in {"prose", "rows", "figures"}, section.key
        assert section.description


def test_the_template_sections_are_built_from_the_skeleton() -> None:
    """One definition, so a section added to the skeleton appears on every
    template rather than needing a second list kept in step."""
    assert [s["key"] for s in section_specs()] == [s.key for s in SECTIONS]


def test_every_shipped_template_puts_its_fields_in_a_real_section() -> None:
    for spec in TEMPLATES:
        for shipped_field in spec["fields"]:
            assert shipped_field["section"] in SECTIONS_BY_KEY, (
                f"{spec['key']}.{shipped_field['key']}"
            )


def test_template_field_keys_are_unique_within_a_template() -> None:
    for spec in TEMPLATES:
        keys = [f["key"] for f in spec["fields"]]
        assert len(set(keys)) == len(keys), spec["key"]


def test_presales_ships_a_daily_and_a_weekly() -> None:
    keys = {spec["key"] for spec in TEMPLATES}
    assert {"report_presales_daily", "report_presales_weekly"} <= keys


def test_the_generic_template_asks_nothing_extra() -> None:
    """The fallback is a real template with the six sections and no questions,
    so a new team can file the day it exists rather than being a special case
    in the code that then needs testing separately."""
    generic = next(t for t in TEMPLATES if t["key"] == "team_report")
    assert generic["fields"] == []


def test_the_weekly_asks_what_a_day_is_too_short_to_answer() -> None:
    weekly = next(t for t in TEMPLATES if t["key"] == "report_presales_weekly")
    keys = {f["key"] for f in weekly["fields"]}
    assert {"bids_won", "bids_lost", "loss_reasons", "pipeline_value"} <= keys


def test_every_completion_has_a_label() -> None:
    assert set(COMPLETIONS) == set(COMPLETION_LABELS)


def test_dropped_is_kept_apart_from_done() -> None:
    """Work that stopped being worth doing is worth saying so about, and
    lumping it in with done hides it."""
    assert "dropped" in COMPLETIONS
    assert COMPLETION_LABELS["dropped"] == "Dropped"


def test_blocked_is_a_severity_of_its_own() -> None:
    assert IssueSeverity.BLOCKED in set(IssueSeverity)


# ── the team's own questions ───────────────────────────────────────────


class _Template:
    """Just enough of a FormTemplate for the pure answer rules."""

    def __init__(self, *keys: str) -> None:
        self.fields = [{"key": k, "label": k, "section": "metrics"} for k in keys]


def test_answers_are_merged_not_replaced() -> None:
    """Filling a long form over two saves must not wipe the first save — and
    an assistant sends one answer at a time as it learns it."""
    template = _Template("quotes_sent", "bids_submitted")
    first = merge_answers(template, {}, {"quotes_sent": 4})
    second = merge_answers(template, first, {"bids_submitted": 2})
    assert second == {"quotes_sent": 4, "bids_submitted": 2}


def test_an_empty_value_clears_one_answer() -> None:
    """The only way to take an answer back once it has been given."""
    template = _Template("quotes_sent", "bids_submitted")
    stored = {"quotes_sent": 4, "bids_submitted": 2}
    assert merge_answers(template, stored, {"quotes_sent": None}) == {
        "bids_submitted": 2
    }


def test_a_question_the_template_does_not_ask_is_dropped() -> None:
    """A stale frontend or a creative model cannot widen what a report holds."""
    template = _Template("quotes_sent")
    assert merge_answers(template, {}, {"salary": 90000}) == {}


def test_an_answer_to_a_question_since_removed_is_dropped_too() -> None:
    """A template edited to drop a field should stop carrying its answer."""
    assert merge_answers(_Template("quotes_sent"), {"gone": 1}, {}) == {}


def test_submitting_names_what_is_still_missing() -> None:
    template = _Template("quotes_sent")
    template.fields[0]["required"] = True
    template.fields[0]["label"] = "Quotes sent this week"
    with pytest.raises(Exception) as caught:
        validate_answers(template, {}, require_required=True)
    assert "Quotes sent this week" in str(caught.value)


def test_a_draft_is_not_nagged_about_required_answers() -> None:
    """A draft that refuses to save because a box is empty is a draft nobody
    keeps."""
    template = _Template("quotes_sent")
    template.fields[0]["required"] = True
    assert validate_answers(template, {}, require_required=False) == {}


# ── who the email goes to ──────────────────────────────────────────────


def test_a_mistyped_address_is_dropped_and_the_rest_kept() -> None:
    """Refusing the whole save over one typo would lose the other nine;
    dropping it silently would hide the mistake."""
    assert _clean_emails(
        ["Ceo@Hamdaz.com", "not-an-address", "  ops@hamdaz.com  ", "ceo@hamdaz.com"]
    ) == ["ceo@hamdaz.com", "ops@hamdaz.com"]


def test_no_addresses_at_all_is_an_empty_list_not_a_crash() -> None:
    assert _clean_emails(None) == []
    assert _clean_emails([]) == []
