"""Who may see which project, and who may change it.

The rule in one line: **you see the projects you are on, all of your team's if
you run it, and everyone's if you run the company.**

Pure functions over already-loaded facts, exactly as the reports module does it
and for the same reason: the assistant reaches projects through the same routes
that a person does, so a rule written twice would eventually be two rules. A
reader who wants to know who can see what should be able to read this file and
stop.

**Three powers, kept apart.** Reading a project, managing its plan, and moving
one task are different things, and a great deal of the value of this module is
that the third does not imply the first two in reverse. An engineer updates
their own task without being able to reschedule the project; a team lead
reschedules without the CEO's reach across teams. Collapsing any pair of these
into one check is how "everyone can edit everything" arrives by accident.

**Assignment implies membership.** There is deliberately no "you can see this
project because you hold a task in it" branch below. Assigning somebody a task
adds them to the project — see ``service.assign`` — so the visibility question
has one answer and not two that can disagree. A task somebody cannot see is a
task nobody does.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Final

from app.models.project import Project, ProjectRole
from app.roles.catalogue import SUPER_ADMIN

#: Global roles that see every team's projects. The same three the reports
#: module treats as company-wide, and for the same reason: somebody accountable
#: for the whole organisation cannot be asked to be added to each project
#: individually. Note that an accountant is not here — reading the accounts is
#: a different question from reading what the AI team is building.
COMPANY_WIDE: Final[frozenset[str]] = frozenset({SUPER_ADMIN, "ceo", "manager"})

#: Team-scoped roles that carry authority over their own team's projects. Held
#: per team, so holding one says nothing about any other team — which is the
#: whole point of them being team-scoped. These are the "team lead or super
#: user" of the brief: the people who may create a project at all.
TEAM_OVERSIGHT: Final[frozenset[str]] = frozenset({"team_manager", "team_lead"})


@dataclass(frozen=True, slots=True)
class Viewer:
    """The caller, as the projects module needs them. Gathered once per request.

    ``member_of`` is carried rather than looked up per project because the
    listing needs it as a SQL filter, and a set built once is both cheaper and
    impossible to get inconsistent with the per-row checks below.
    """

    user_id: uuid.UUID
    #: Global role keys.
    roles: frozenset[str]
    #: Teams they belong to at all.
    team_ids: frozenset[uuid.UUID]
    #: Teams they run — not merely belong to.
    oversees: frozenset[uuid.UUID]
    #: Projects they are on, whatever their role there.
    member_of: frozenset[uuid.UUID]
    #: Projects they are on *as lead*. A subset of ``member_of``; kept apart
    #: because leading is the power to re-plan and membership is not.
    leads: frozenset[uuid.UUID]

    @property
    def is_company_wide(self) -> bool:
        return not COMPANY_WIDE.isdisjoint(self.roles)

    @property
    def is_super_admin(self) -> bool:
        return SUPER_ADMIN in self.roles


def member_role(project: Project, user_id: uuid.UUID) -> str | None:
    """What this person is on this project, or ``None`` if they are not on it.

    Reads ``project.members``, which is eagerly loaded — see the model. Safe to
    call from synchronous code inside an async request for that reason, and it
    is the only reason.
    """
    for row in project.members:
        if row.user_id == user_id:
            return row.role
    return None


def may_read(project: Project, viewer: Viewer) -> bool:
    """Whether this person may open this project at all.

    Being on the project is enough, in any role including viewer. A project is
    not a secret from the people doing it, and a member who could hold a task
    but not read the plan around it would have to be told what they are
    contributing to by email — which is what this module exists to stop.
    """
    if viewer.is_company_wide:
        return True
    if project.team_id in viewer.oversees:
        return True
    if project.lead_id == viewer.user_id:
        return True
    return project.id in viewer.member_of or member_role(project, viewer.user_id) is not None


def may_manage(project: Project, viewer: Viewer) -> bool:
    """Whether they may change the plan: the project itself, its milestones,
    its tasks and who is on it.

    The project's own lead counts, and so does anybody running the team it
    belongs to. An ordinary member does not — they move their own work, which
    is ``may_update_task`` below. That distinction is the difference between a
    plan and a shared document.

    An archived project is managed by nobody. Restoring it is a separate,
    deliberate act, and it is the one thing ``may_administer`` still allows.
    """
    if project.is_archived:
        return False
    if viewer.is_company_wide:
        return True
    if project.team_id in viewer.oversees:
        return True
    if project.lead_id == viewer.user_id:
        return True
    return member_role(project, viewer.user_id) == ProjectRole.LEAD


def may_administer(project: Project, viewer: Viewer) -> bool:
    """Archive, restore or delete. Narrower than managing, and on purpose.

    A project lead runs their project; removing it from the record is a
    different kind of act, and one whose consequences reach the reports filed
    against it. Left with the people who are accountable for the team rather
    than for the work.
    """
    return viewer.is_company_wide or project.team_id in viewer.oversees


def may_update_task(project: Project, task: Any, viewer: Viewer) -> bool:
    """Whether they may move this one task along.

    The assignee, or anybody who manages the project. This is the permission
    that makes the module useful to the people actually doing the work: an
    engineer records progress on what they hold without being able to touch the
    schedule, and their update is the same shape as their lead's, so a report
    reads the same either way.

    Note that it does **not** let an assignee reassign their task to somebody
    else. Handing work on is a planning decision; a person who wants out of a
    task says so, and the lead moves it.
    """
    if may_manage(project, viewer):
        return True
    return task.assignee_id is not None and task.assignee_id == viewer.user_id


def may_create(team_id: uuid.UUID, viewer: Viewer) -> bool:
    """Whether they may start a project for this team.

    Oversight, not membership — the opposite of the reports module, where
    filing is what an ordinary member does. Creating a project commits other
    people's time, so it sits with whoever runs the team. A super admin may
    create anywhere, which is what makes the feature testable against a real
    team without first joining it.
    """
    return viewer.is_super_admin or viewer.is_company_wide or team_id in viewer.oversees


def may_report_on(project: Project, viewer: Viewer) -> bool:
    """Whether they may file a status report about this project.

    The same people who may manage it. A status report is an account of the
    plan given by whoever is answerable for it — a member filing one about
    somebody else's project would be reporting on work they do not run, and the
    report would still carry the project's name against their judgement.
    """
    return may_manage(project, viewer)


def visible_team_ids(viewer: Viewer) -> frozenset[uuid.UUID] | None:
    """Teams whose every project this person may read, or ``None`` for all.

    ``None`` rather than an enumerated set so a query can leave the clause off
    entirely, and so a team created after the request began does not quietly
    become invisible to the CEO.

    A caller filtering a listing must combine this with ``viewer.member_of``:
    the two together are the readable set, because somebody can be on one
    project of a team they do not otherwise see.
    """
    if viewer.is_company_wide:
        return None
    return viewer.oversees
