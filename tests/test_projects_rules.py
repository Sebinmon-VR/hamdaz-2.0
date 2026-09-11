"""The project rules, decided without a database.

Two halves, and both are pure functions over already-loaded rows, so every case
here runs in milliseconds and is checked the same way however the request
arrived — the board, the API, or the assistant acting on somebody's behalf.

**Who may see and change what** is the half worth testing hardest. A project
carries what a team is building, who is late and what is blocked, and the
module's whole shape rests on three powers being kept apart: reading a project,
running its plan, and moving one task. Most of the cases below exist to pin the
gaps between those three, because that is where an access model quietly stops
meaning anything.

**What the numbers mean** is the other half. Percent complete, what counts as
overdue, and what the dials would say are each defined once in
``app.projects.progress`` and used by the board, the API and every status
report. If they were wrong here they would be wrong identically in three
places, which is exactly why they are tested here and not through a route.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.periods import GRAINS, window_for, window_label
from app.models.project import (
    MilestonePlan,
    ProjectRole,
    ProjectStatus,
    Rag,
    TaskStatus,
)
from app.projects.access import (
    COMPANY_WIDE,
    TEAM_OVERSIGHT,
    Viewer,
    may_administer,
    may_create,
    may_manage,
    may_read,
    may_report_on,
    may_update_task,
    member_role,
    visible_team_ids,
)
from app.projects.progress import (
    STALE_AFTER_DAYS,
    cost_suggestion,
    health_is_stale,
    milestone_percent,
    milestone_state,
    percent_of_tasks,
    project_percent,
    rollup,
    schedule_suggestion,
    slip_days,
    task_is_overdue,
)

TEAM = uuid.uuid4()
OTHER_TEAM = uuid.uuid4()
PROJECT = uuid.uuid4()
LEAD = uuid.uuid4()
MEMBER = uuid.uuid4()
STRANGER = uuid.uuid4()

TODAY = date(2026, 9, 10)
NOW = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)


# ── fakes ──────────────────────────────────────────────────────────────
#
# Plain objects rather than ORM instances. Every function under test takes
# duck-typed rows on purpose — that is what lets a status report snapshot a
# project without the reports module importing this one — so testing them with
# real models would be testing SQLAlchemy rather than the rules.


class FakeMember:
    def __init__(self, user_id: uuid.UUID, role: str = ProjectRole.MEMBER) -> None:
        self.user_id = user_id
        self.role = role


class FakeTask:
    def __init__(
        self,
        *,
        status: str = TaskStatus.IN_PROGRESS,
        percent: int = 0,
        due_on: date | None = None,
        milestone_id: uuid.UUID | None = None,
        assignee_id: uuid.UUID | None = None,
        estimate_hours: Decimal | None = None,
    ) -> None:
        self.id = uuid.uuid4()
        self.status = status
        self.percent_complete = percent
        self.due_on = due_on
        self.milestone_id = milestone_id
        self.assignee_id = assignee_id
        self.estimate_hours = estimate_hours


class FakeMilestone:
    def __init__(
        self,
        *,
        due_on: date | None = None,
        done_on: date | None = None,
        percent: int = 0,
        plan: str = MilestonePlan.ON_PLAN,
        baseline: date | None = None,
    ) -> None:
        self.id = uuid.uuid4()
        self.due_on = due_on
        self.done_on = done_on
        self.percent_complete = percent
        self.plan = plan
        self.baseline_due_on = baseline

    @property
    def is_done(self) -> bool:
        return self.done_on is not None or self.percent_complete >= 100


class FakeIssue:
    def __init__(self, *, status: str = "open", needs_support: bool = False) -> None:
        self.status = status
        self.needs_support = needs_support


class FakeProject:
    def __init__(
        self,
        *,
        team_id: uuid.UUID = TEAM,
        lead_id: uuid.UUID | None = LEAD,
        members: list[FakeMember] | None = None,
        percent_complete: int | None = None,
        archived: bool = False,
        budget: Decimal | None = None,
        spend: Decimal | None = None,
        reviewed_at: datetime | None = NOW,
        status: str = ProjectStatus.ACTIVE,
    ) -> None:
        self.id = PROJECT
        self.team_id = team_id
        self.lead_id = lead_id
        self.members = members if members is not None else []
        self.percent_complete = percent_complete
        self.archived_at = datetime(2026, 1, 1, tzinfo=UTC) if archived else None
        self.budget_amount = budget
        self.spend_amount = spend
        self.health_reviewed_at = reviewed_at
        self.status = status

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None


def viewer(
    *,
    user_id: uuid.UUID = STRANGER,
    roles: set[str] | None = None,
    teams: set[uuid.UUID] | None = None,
    oversees: set[uuid.UUID] | None = None,
    member_of: set[uuid.UUID] | None = None,
    leads: set[uuid.UUID] | None = None,
) -> Viewer:
    return Viewer(
        user_id=user_id,
        roles=frozenset(roles or set()),
        team_ids=frozenset(teams or set()),
        oversees=frozenset(oversees or set()),
        member_of=frozenset(member_of or set()),
        leads=frozenset(leads or set()),
    )


# ── who may see a project ──────────────────────────────────────────────


def test_a_stranger_sees_nothing() -> None:
    """Not on it, does not run the team, does not run the company."""
    assert may_read(FakeProject(), viewer()) is False


def test_being_on_a_project_is_enough_to_read_it() -> None:
    """Including as a viewer. A project is not a secret from the people doing it."""
    for role in (ProjectRole.LEAD, ProjectRole.MEMBER, ProjectRole.VIEWER):
        project = FakeProject(members=[FakeMember(MEMBER, role)])
        assert may_read(project, viewer(user_id=MEMBER)) is True, role


def test_running_the_team_shows_every_project_in_it() -> None:
    who = viewer(user_id=STRANGER, oversees={TEAM})
    assert may_read(FakeProject(team_id=TEAM), who) is True
    assert may_read(FakeProject(team_id=OTHER_TEAM), who) is False


@pytest.mark.parametrize("role", sorted(COMPANY_WIDE))
def test_running_the_company_shows_everything(role: str) -> None:
    who = viewer(roles={role})
    assert may_read(FakeProject(team_id=OTHER_TEAM), who) is True


def test_merely_belonging_to_the_team_is_not_enough() -> None:
    """Being in the AI team does not put every AI project on your screen.

    ``team_ids`` is membership and ``oversees`` is authority. Only the second
    one carries a right to other people's work.
    """
    who = viewer(teams={TEAM}, oversees=set())
    assert may_read(FakeProject(team_id=TEAM), who) is False


def test_the_named_lead_reads_it_even_without_a_member_row() -> None:
    """Belt and braces: ``lead_id`` and the member row should agree, and the
    rule does not fall over in the window where they do not."""
    project = FakeProject(lead_id=LEAD, members=[])
    assert may_read(project, viewer(user_id=LEAD)) is True


# ── who may change the plan ────────────────────────────────────────────


def test_a_member_reads_but_does_not_replan() -> None:
    """The distinction the whole module rests on."""
    project = FakeProject(lead_id=LEAD, members=[FakeMember(MEMBER)])
    who = viewer(user_id=MEMBER, member_of={PROJECT})
    assert may_read(project, who) is True
    assert may_manage(project, who) is False


def test_the_project_lead_runs_the_plan() -> None:
    project = FakeProject(lead_id=LEAD, members=[FakeMember(LEAD, ProjectRole.LEAD)])
    assert may_manage(project, viewer(user_id=LEAD)) is True


def test_a_team_lead_runs_their_teams_plans() -> None:
    who = viewer(oversees={TEAM})
    assert may_manage(FakeProject(team_id=TEAM), who) is True
    assert may_manage(FakeProject(team_id=OTHER_TEAM), who) is False


def test_an_archived_project_is_managed_by_nobody() -> None:
    """Not even a super admin. Restoring it is the separate, deliberate act —
    and that is the one thing ``may_administer`` still allows."""
    project = FakeProject(archived=True)
    boss = viewer(roles={"super_admin"})
    assert may_manage(project, boss) is False
    assert may_administer(project, boss) is True


def test_a_project_lead_cannot_delete_their_own_project() -> None:
    """Running a project and removing it from the record are different powers.

    The second reaches the reports already filed against it, so it stays with
    whoever is accountable for the team.
    """
    project = FakeProject(lead_id=LEAD, members=[FakeMember(LEAD, ProjectRole.LEAD)])
    assert may_manage(project, viewer(user_id=LEAD)) is True
    assert may_administer(project, viewer(user_id=LEAD)) is False


# ── who may move one task ──────────────────────────────────────────────


def test_an_assignee_updates_their_own_task() -> None:
    """The permission that makes the module useful to the people doing the work."""
    project = FakeProject(members=[FakeMember(MEMBER)])
    task = FakeTask(assignee_id=MEMBER)
    assert may_update_task(project, task, viewer(user_id=MEMBER, member_of={PROJECT})) is True


def test_an_assignee_does_not_get_everybody_elses_tasks() -> None:
    project = FakeProject(members=[FakeMember(MEMBER)])
    someone_elses = FakeTask(assignee_id=uuid.uuid4())
    assert (
        may_update_task(project, someone_elses, viewer(user_id=MEMBER, member_of={PROJECT}))
        is False
    )


def test_an_unassigned_task_belongs_to_nobody() -> None:
    """A null assignee must not match a null anything. This is the case that
    would quietly hand every unallocated task to everyone."""
    project = FakeProject(members=[FakeMember(MEMBER)])
    orphan = FakeTask(assignee_id=None)
    who = viewer(user_id=MEMBER, member_of={PROJECT})
    assert may_update_task(project, orphan, who) is False


def test_the_lead_may_move_anybodys_task() -> None:
    project = FakeProject(lead_id=LEAD)
    assert may_update_task(project, FakeTask(assignee_id=MEMBER), viewer(user_id=LEAD)) is True


# ── who may start one, and who may report on it ────────────────────────


def test_starting_a_project_takes_oversight_not_membership() -> None:
    """The opposite of filing a report, and deliberately: a project commits
    other people's time."""
    assert may_create(TEAM, viewer(teams={TEAM})) is False
    assert may_create(TEAM, viewer(oversees={TEAM})) is True
    assert may_create(TEAM, viewer(roles={"super_admin"})) is True


def test_reporting_on_a_project_follows_running_it() -> None:
    """A member filing a status report would be reporting on work they do not
    run, and the report would still carry the project's name."""
    project = FakeProject(lead_id=LEAD, members=[FakeMember(MEMBER)])
    assert may_report_on(project, viewer(user_id=LEAD)) is True
    assert may_report_on(project, viewer(user_id=MEMBER, member_of={PROJECT})) is False


def test_the_visible_team_set_is_none_for_company_wide_roles() -> None:
    """``None`` rather than an enumerated set, so a team created after the
    request began does not quietly become invisible to the CEO."""
    assert visible_team_ids(viewer(roles={"ceo"})) is None
    assert visible_team_ids(viewer(oversees={TEAM})) == frozenset({TEAM})


def test_team_oversight_roles_are_the_team_scoped_ones() -> None:
    """Held per team, so holding one says nothing about any other team."""
    assert frozenset({"team_manager", "team_lead"}) == TEAM_OVERSIGHT
    assert COMPANY_WIDE.isdisjoint(TEAM_OVERSIGHT)


def test_member_role_reads_the_loaded_rows() -> None:
    project = FakeProject(members=[FakeMember(MEMBER, ProjectRole.VIEWER)])
    assert member_role(project, MEMBER) == ProjectRole.VIEWER
    assert member_role(project, STRANGER) is None


# ── how far along ──────────────────────────────────────────────────────


def test_no_tasks_is_none_rather_than_zero() -> None:
    """"Nothing planned" and "nothing started" are very different states for a
    manager reading a portfolio."""
    assert percent_of_tasks([]) is None


def test_every_task_counts_once_when_estimates_are_missing() -> None:
    tasks = [FakeTask(percent=100), FakeTask(percent=0), FakeTask(percent=50)]
    assert percent_of_tasks(tasks) == 50


def test_estimates_are_used_only_when_every_task_has_one() -> None:
    """A half-estimated plan weighted by hours reads as precision it does not
    have — three unestimated tasks would each be worth a fortieth of a
    forty-hour one purely because nobody filled the box in."""
    weighted = [
        FakeTask(percent=100, estimate_hours=Decimal("40")),
        FakeTask(percent=0, estimate_hours=Decimal("10")),
    ]
    assert percent_of_tasks(weighted) == 80

    partial = [
        FakeTask(percent=100, estimate_hours=Decimal("40")),
        FakeTask(percent=0),
    ]
    assert percent_of_tasks(partial) == 50


def test_dropping_a_task_moves_the_percentage_up() -> None:
    """Dropped work stopped existing; it did not fail. A denominator that kept
    counting it would punish a team for tidying up their plan."""
    before = [FakeTask(percent=100), FakeTask(percent=0)]
    assert percent_of_tasks(before) == 50

    after = [FakeTask(percent=100), FakeTask(percent=0, status=TaskStatus.DROPPED)]
    assert percent_of_tasks(after) == 100


def test_a_milestone_is_as_far_along_as_the_work_under_it() -> None:
    stone = FakeMilestone(percent=10)
    tasks = [
        FakeTask(percent=100, milestone_id=stone.id),
        FakeTask(percent=50, milestone_id=stone.id),
        FakeTask(percent=0, milestone_id=uuid.uuid4()),  # a different milestone
    ]
    assert milestone_percent(stone, tasks) == 75


def test_a_milestone_with_no_tasks_keeps_what_was_typed() -> None:
    """How a plan sketched before it is broken down still shows movement."""
    assert milestone_percent(FakeMilestone(percent=40), []) == 40


def test_a_finished_milestone_with_no_tasks_is_a_hundred() -> None:
    assert milestone_percent(FakeMilestone(done_on=TODAY), []) == 100


def test_the_leads_own_figure_wins() -> None:
    """They know things the rows do not."""
    project = FakeProject(percent_complete=25)
    assert project_percent(project, [FakeTask(percent=100)], []) == 25


def test_tasks_decide_when_the_lead_has_not_said() -> None:
    project = FakeProject(percent_complete=None)
    assert project_percent(project, [FakeTask(percent=100), FakeTask(percent=0)], []) == 50


def test_milestones_decide_when_there_are_no_tasks() -> None:
    """A project planned only to milestone level still reports progress."""
    project = FakeProject(percent_complete=None)
    stones = [FakeMilestone(percent=100), FakeMilestone(percent=0)]
    assert project_percent(project, [], stones) == 50


def test_an_empty_project_is_zero() -> None:
    assert project_percent(FakeProject(percent_complete=None), [], []) == 0


# ── whether it is late ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "stone,expected",
    [
        (FakeMilestone(done_on=date(2026, 8, 1)), "done"),
        (FakeMilestone(percent=100), "done"),
        (FakeMilestone(due_on=None), "undated"),
        (FakeMilestone(due_on=date(2026, 9, 9)), "overdue"),
        (FakeMilestone(due_on=date(2026, 9, 10)), "due"),
        (FakeMilestone(due_on=date(2026, 9, 17)), "due"),
        (FakeMilestone(due_on=date(2026, 9, 18)), "upcoming"),
    ],
)
def test_where_a_milestone_stands(stone: FakeMilestone, expected: str) -> None:
    """Derived from the dates at read time, so a plan stays truthful without a
    nightly job walking every row to notice that yesterday happened."""
    assert milestone_state(stone, TODAY) == expected


def test_a_finished_milestone_is_never_overdue() -> None:
    """Done beats late, whatever its date says."""
    late_but_done = FakeMilestone(due_on=date(2026, 1, 1), done_on=date(2026, 2, 1))
    assert milestone_state(late_but_done, TODAY) == "done"


def test_slip_is_none_when_a_milestone_has_never_moved() -> None:
    """Not zero. Zero would mean it was rescheduled onto the same day, and only
    one of those is worth a line on a report."""
    assert slip_days(FakeMilestone(due_on=TODAY, baseline=None)) is None
    assert slip_days(FakeMilestone(due_on=TODAY, baseline=TODAY)) == 0
    assert slip_days(FakeMilestone(due_on=date(2026, 9, 24), baseline=TODAY)) == 14


def test_a_done_task_is_never_overdue() -> None:
    """Judged on the date, but only for work that is still open."""
    stale = date(2026, 1, 1)
    assert task_is_overdue(FakeTask(due_on=stale), TODAY) is True
    assert task_is_overdue(FakeTask(due_on=stale, status=TaskStatus.DONE), TODAY) is False
    assert task_is_overdue(FakeTask(due_on=stale, status=TaskStatus.DROPPED), TODAY) is False
    assert task_is_overdue(FakeTask(due_on=None), TODAY) is False


# ── what the dials would say ───────────────────────────────────────────


def test_an_overdue_milestone_with_impact_suggests_red() -> None:
    stones = [
        FakeMilestone(due_on=date(2026, 8, 1), plan=MilestonePlan.OFF_PLAN_IMPACT)
    ]
    hint = schedule_suggestion(stones, [], TODAY)
    assert hint.rag == Rag.RED
    assert "impact" in hint.reason


def test_an_overdue_milestone_without_impact_suggests_amber() -> None:
    stones = [FakeMilestone(due_on=date(2026, 8, 1))]
    assert schedule_suggestion(stones, [], TODAY).rag == Rag.AMBER


def test_an_empty_plan_suggests_nothing() -> None:
    """Grey, not green. There is nothing to be optimistic about yet."""
    hint = schedule_suggestion([], [], TODAY)
    assert hint.rag == Rag.GREY
    assert hint.reason == "nothing planned yet"


def test_a_plan_that_is_on_time_suggests_green() -> None:
    stones = [FakeMilestone(due_on=date(2026, 12, 1))]
    assert schedule_suggestion(stones, [], TODAY).rag == Rag.GREEN


def test_spend_is_judged_against_progress_not_the_calendar() -> None:
    """80% spent is fine at 80% done and alarming at 20% done."""
    fine = FakeProject(budget=Decimal("100"), spend=Decimal("80"), percent_complete=80)
    assert cost_suggestion(fine).rag == Rag.GREEN

    alarming = FakeProject(budget=Decimal("100"), spend=Decimal("80"), percent_complete=20)
    assert cost_suggestion(alarming).rag == Rag.RED


def test_no_budget_recorded_says_nothing() -> None:
    """Green would be a statement about money nobody has entered."""
    hint = cost_suggestion(FakeProject(budget=None, spend=None))
    assert hint.rag == Rag.GREY
    assert hint.reason == "no budget recorded"


def test_overspend_is_red_however_far_along_it_is() -> None:
    over = FakeProject(budget=Decimal("100"), spend=Decimal("140"), percent_complete=100)
    assert cost_suggestion(over).rag == Rag.RED


# ── stale dials ────────────────────────────────────────────────────────


def test_a_never_reviewed_project_is_stale_from_the_start() -> None:
    """Grey dials nobody has confirmed are not evidence of anything."""
    assert health_is_stale(FakeProject(reviewed_at=None), NOW) is True


def test_dials_go_stale_after_a_fortnight() -> None:
    fresh = FakeProject(reviewed_at=NOW - timedelta(days=STALE_AFTER_DAYS - 1))
    old = FakeProject(reviewed_at=NOW - timedelta(days=STALE_AFTER_DAYS))
    assert health_is_stale(fresh, NOW) is False
    assert health_is_stale(old, NOW) is True


# ── the roll-up ────────────────────────────────────────────────────────


def test_the_rollup_counts_everything_in_one_pass() -> None:
    """One set of figures, built once and handed to the card, the API and a
    report's project line. Three call sites counting the same rows three times
    is how they end up disagreeing by one."""
    stone = FakeMilestone(due_on=date(2026, 8, 1))
    done_stone = FakeMilestone(done_on=date(2026, 7, 1))
    tasks = [
        FakeTask(status=TaskStatus.DONE, percent=100),
        FakeTask(status=TaskStatus.BLOCKED, percent=30, due_on=date(2026, 1, 1)),
        FakeTask(status=TaskStatus.IN_PROGRESS, percent=50),
        FakeTask(status=TaskStatus.DROPPED, percent=0),
    ]
    issues = [
        FakeIssue(status="open", needs_support=True),
        FakeIssue(status="in_progress"),
        FakeIssue(status="resolved"),
    ]
    figures = rollup(
        FakeProject(percent_complete=None), tasks, [stone, done_stone], issues, TODAY
    )

    assert figures.tasks_total == 4
    assert figures.tasks_done == 1
    assert figures.tasks_open == 2
    assert figures.tasks_blocked == 1
    assert figures.tasks_overdue == 1
    assert figures.milestones_total == 2
    assert figures.milestones_done == 1
    assert figures.milestones_overdue == 1
    # A resolved issue is not open; a dropped task is not counted at all.
    assert figures.issues_open == 2
    assert figures.issues_needing_support == 1
    assert figures.percent_complete == 60  # (100 + 30 + 50) / 3, dropped excluded


# ── reporting windows ──────────────────────────────────────────────────


@pytest.mark.parametrize("grain", GRAINS)
@pytest.mark.parametrize(
    "day", [date(2026, 1, 1), date(2026, 2, 14), date(2026, 6, 30), date(2026, 12, 31)]
)
def test_a_window_always_contains_the_day_it_was_asked_about(
    grain: str, day: date
) -> None:
    start, end = window_for(grain, day)
    assert start <= day <= end


def test_a_week_runs_monday_to_sunday() -> None:
    # 2026-09-10 is a Thursday.
    assert window_for("week", TODAY) == (date(2026, 9, 7), date(2026, 9, 13))


@pytest.mark.parametrize(
    "day,expected",
    [
        (date(2026, 1, 15), (date(2026, 1, 1), date(2026, 3, 31))),
        (date(2026, 5, 31), (date(2026, 4, 1), date(2026, 6, 30))),
        (date(2026, 7, 1), (date(2026, 7, 1), date(2026, 9, 30))),
        # The rollover case: Q4 ends at the year boundary, not in the next year.
        (date(2026, 12, 31), (date(2026, 10, 1), date(2026, 12, 31))),
    ],
)
def test_quarters_are_calendar_quarters(day: date, expected: tuple) -> None:
    assert window_for("quarter", day) == expected


def test_a_leap_february_ends_on_the_29th() -> None:
    assert window_for("month", date(2028, 2, 14)) == (date(2028, 2, 1), date(2028, 2, 29))


def test_a_year_is_the_calendar_year() -> None:
    assert window_for("year", TODAY) == (date(2026, 1, 1), date(2026, 12, 31))


def test_a_custom_window_is_one_day_until_the_caller_says_otherwise() -> None:
    """Nothing else can know what they meant, so it does not guess."""
    assert window_for("custom", TODAY) == (TODAY, TODAY)


def test_a_window_reads_as_a_person_would_say_it() -> None:
    assert window_label("month", date(2026, 9, 1), date(2026, 9, 30)) == "September 2026"
    assert window_label("quarter", date(2026, 10, 1), date(2026, 12, 31)) == "Q4 2026"
    assert window_label("year", date(2026, 1, 1), date(2026, 12, 31)) == "2026"
    assert "Thursday" in window_label("day", TODAY, TODAY)
