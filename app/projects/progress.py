"""How far along things are, and what the dials probably ought to say.

Pure functions over rows that are already loaded. Nothing here touches a
session, which means every rule below can be tested without a database and —
more importantly — that the board, the API and a status report all reach the
same number by calling the same function rather than by three people
independently deciding what "percent complete" means.

Two kinds of thing live here:

* **Derivations** — the percentage of a milestone or a project, and whether a
  milestone has slipped. These are facts about the rows, and nobody types them.
* **Suggestions** — what the schedule and cost dials would say if they were
  computed. They are *not* written to the project. A lead's judgement is the
  stored value and this is shown beside it, because a project can be behind and
  green for a reason a date arithmetic cannot know, and a dial that overwrites
  itself is a dial nobody trusts.

The one that is neither is ``window_for``, which is here rather than in the
reports module because a project report and a project dashboard need the same
answer to "what does 'this week' mean" and only one of them is a report.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Final, Literal

from app.core.periods import GRAINS, Grain, window_for, window_label
from app.models.project import (
    OPEN_ISSUE_STATUSES,
    OPEN_TASK_STATUSES,
    MilestonePlan,
    Rag,
    TaskStatus,
)

# ── what a reporting window is ─────────────────────────────────────────

#: Re-exported from ``app.core.periods`` rather than defined here. The reports
#: module needs the same arithmetic for its own cadences, and two copies of
#: "which days are in this week" agree right up until somebody adds a grain to
#: one of them. Imported through this module so callers in the projects package
#: have one obvious place to look.
__all_periods__ = (Grain, GRAINS, window_for, window_label)


# ── how far along ──────────────────────────────────────────────────────

#: Tasks that are no longer work and so are not part of the denominator.
#: Dropping a task should move a project's percentage *up*, never down — it is
#: work that stopped existing, not work that failed.
_NOT_COUNTED: Final[frozenset[str]] = frozenset({TaskStatus.DROPPED})


def countable(tasks: Iterable[Any]) -> list[Any]:
    return [t for t in tasks if t.status not in _NOT_COUNTED]


def percent_of_tasks(tasks: Iterable[Any]) -> int | None:
    """The mean completion of a set of tasks, or ``None`` if there are none.

    **Every task counts once**, and estimates are used only when *all* of them
    carry one. A half-estimated plan weighted by hours reads as precision it
    does not have — a forty-hour task next to three unestimated ones would make
    those three worth a fortieth of it each, purely because nobody filled the
    box in. Equal weighting is wrong in a way people can see and reason about;
    partial weighting is wrong in a way they cannot.

    ``None`` rather than 0 for an empty plan, so "no tasks yet" and "nothing
    started" stay distinguishable — they are very different states for a
    manager reading a portfolio.
    """
    rows = countable(tasks)
    if not rows:
        return None

    weights = [_estimate(t) for t in rows]
    if all(w is not None and w > 0 for w in weights):
        total = sum(w for w in weights)  # type: ignore[misc]
        done = sum(
            w * t.percent_complete
            for w, t in zip(weights, rows, strict=True)  # type: ignore[operator]
        )
        return int(round(float(done) / float(total)))

    return int(round(sum(t.percent_complete for t in rows) / len(rows)))


def _estimate(task: Any) -> Decimal | None:
    value = getattr(task, "estimate_hours", None)
    return value if value is not None and value > 0 else None


def milestone_percent(milestone: Any, tasks: Iterable[Any]) -> int:
    """A milestone's completion: from its own tasks when it has any.

    A milestone with work under it is as far along as that work, and the stored
    figure is ignored — two numbers claiming to be the same thing is worse than
    one that is occasionally coarse. A milestone with no tasks keeps whatever
    was typed on it, which is how a plan sketched before it is broken down
    still shows movement.
    """
    mine = [t for t in tasks if getattr(t, "milestone_id", None) == milestone.id]
    derived = percent_of_tasks(mine)
    if derived is not None:
        return derived
    return 100 if milestone.done_on is not None else milestone.percent_complete


def project_percent(project: Any, tasks: Iterable[Any], milestones: Iterable[Any]) -> int:
    """A project's completion, in the order of preference that respects people.

    The lead's own figure wins when they set one — they know things the rows do
    not. Failing that the tasks decide, and failing *those* the milestones do,
    so a project planned only to milestone level still reports progress. A
    project with nothing in it at all is 0, which here genuinely means nothing
    has started.
    """
    if project.percent_complete is not None:
        return project.percent_complete

    rows = list(tasks)
    derived = percent_of_tasks(rows)
    if derived is not None:
        return derived

    stones = list(milestones)
    if stones:
        return int(round(sum(milestone_percent(m, rows) for m in stones) / len(stones)))
    return 0


# ── whether it is late ─────────────────────────────────────────────────

MilestoneState = Literal["done", "due", "overdue", "upcoming", "undated"]


def milestone_state(milestone: Any, today: date) -> MilestoneState:
    """Where a milestone stands against its own date.

    Derived rather than stored so a plan stays truthful without a nightly job
    walking every row to notice that yesterday happened.
    """
    if milestone.is_done:
        return "done"
    if milestone.due_on is None:
        return "undated"
    if milestone.due_on < today:
        return "overdue"
    if milestone.due_on <= today + timedelta(days=7):
        return "due"
    return "upcoming"


def slip_days(milestone: Any) -> int | None:
    """How far a milestone has moved from where the plan first put it.

    ``None`` when it has never moved, which is not the same as zero — zero
    would mean it was rescheduled onto the same day, and only one of those is
    worth a line on a report.
    """
    if milestone.baseline_due_on is None or milestone.due_on is None:
        return None
    return (milestone.due_on - milestone.baseline_due_on).days


def task_is_overdue(task: Any, today: date) -> bool:
    """Not done, and the date has passed.

    Judged on the task's own due date and not on any status it carries, for the
    same reason the proposals module derives an effective status: a status says
    whether somebody updated a field, and a date says whether the work is late.
    """
    return task.status in OPEN_TASK_STATUSES and task.due_on is not None and task.due_on < today


# ── what the dials would say ───────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Suggestion:
    """A computed opinion about one dial, and the reason for it.

    The reason is half the value. A screen that says "schedule: amber" invites
    an argument; one that says "amber — 2 milestones overdue" invites a fix.
    """

    rag: str
    reason: str


def schedule_suggestion(
    milestones: Iterable[Any], tasks: Iterable[Any], today: date
) -> Suggestion:
    """Red if anything key has slipped with impact, amber if anything is late.

    Deliberately blunt. This exists to catch the project whose schedule dial
    says green because nobody has looked at it since March, not to grade the
    plan finely — a subtle heuristic here would be arguing with the lead, which
    is not what a suggestion is for.
    """
    stones = list(milestones)
    overdue = [m for m in stones if milestone_state(m, today) == "overdue"]
    impacting = [m for m in overdue if m.plan == MilestonePlan.OFF_PLAN_IMPACT]
    late_tasks = [t for t in tasks if task_is_overdue(t, today)]

    if impacting:
        return Suggestion(
            Rag.RED,
            f"{len(impacting)} overdue milestone{'s' if len(impacting) > 1 else ''} "
            "with knock-on impact",
        )
    if overdue:
        return Suggestion(
            Rag.AMBER,
            f"{len(overdue)} milestone{'s' if len(overdue) > 1 else ''} past its date",
        )
    if len(late_tasks) >= 3:
        return Suggestion(Rag.AMBER, f"{len(late_tasks)} tasks past their date")
    if not stones and not list(tasks):
        return Suggestion(Rag.GREY, "nothing planned yet")
    return Suggestion(Rag.GREEN, "no milestone has passed its date")


def cost_suggestion(project: Any) -> Suggestion:
    """Spend against budget, compared with how far along the work is.

    Being 80% spent is fine at 80% done and alarming at 20% done, so the
    comparison is against progress rather than against the calendar. With no
    budget recorded there is nothing to say, and saying green would be a
    statement about money nobody has entered.
    """
    budget, spend = project.budget_amount, project.spend_amount
    if budget is None or spend is None or budget <= 0:
        return Suggestion(Rag.GREY, "no budget recorded")

    used = float(spend) / float(budget) * 100
    done = project.percent_complete if project.percent_complete is not None else 0
    if used > 100:
        return Suggestion(Rag.RED, f"{used:.0f}% of budget spent")
    if used - done >= 25:
        return Suggestion(Rag.RED, f"{used:.0f}% spent at {done}% complete")
    if used - done >= 10:
        return Suggestion(Rag.AMBER, f"{used:.0f}% spent at {done}% complete")
    return Suggestion(Rag.GREEN, f"{used:.0f}% spent at {done}% complete")


#: How long a set of dials may go unreviewed before the board says so. A
#: fortnight: long enough not to nag a team reporting weekly, short enough that
#: a project nobody has looked at for a month is visibly unreviewed.
STALE_AFTER_DAYS: Final = 14


def health_is_stale(project: Any, now: datetime, *, days: int = STALE_AFTER_DAYS) -> bool:
    """Whether the dials are old enough that they should not be trusted.

    A project never reviewed at all counts as stale from the moment it starts,
    which is the honest answer: grey dials that nobody has confirmed are not
    evidence of anything.
    """
    if project.health_reviewed_at is None:
        return True
    return (now - project.health_reviewed_at).days >= days


# ── the roll-up a card or a report row needs ───────────────────────────


@dataclass(frozen=True, slots=True)
class Rollup:
    """Everything countable about one project, worked out in one pass.

    Built once and handed to whoever needs it — the board card, the portfolio
    table, a report's project line. Three call sites counting the same rows
    three times is how they end up disagreeing by one.
    """

    tasks_total: int
    tasks_done: int
    tasks_open: int
    tasks_blocked: int
    tasks_overdue: int
    milestones_total: int
    milestones_done: int
    milestones_overdue: int
    issues_open: int
    issues_needing_support: int
    percent_complete: int


def rollup(
    project: Any,
    tasks: Iterable[Any],
    milestones: Iterable[Any],
    issues: Iterable[Any],
    today: date,
) -> Rollup:
    rows = list(tasks)
    stones = list(milestones)
    problems = list(issues)
    open_issues = [i for i in problems if i.status in OPEN_ISSUE_STATUSES]

    return Rollup(
        tasks_total=len(rows),
        tasks_done=sum(1 for t in rows if t.status == TaskStatus.DONE),
        tasks_open=sum(1 for t in rows if t.status in OPEN_TASK_STATUSES),
        tasks_blocked=sum(1 for t in rows if t.status == TaskStatus.BLOCKED),
        tasks_overdue=sum(1 for t in rows if task_is_overdue(t, today)),
        milestones_total=len(stones),
        milestones_done=sum(1 for m in stones if m.is_done),
        milestones_overdue=sum(1 for m in stones if milestone_state(m, today) == "overdue"),
        issues_open=len(open_issues),
        issues_needing_support=sum(1 for i in open_issues if i.needs_support),
        percent_complete=project_percent(project, rows, stones),
    )
