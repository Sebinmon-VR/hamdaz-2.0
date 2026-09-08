"""Who may read whose reports.

The rule in one line: **you see your own, your team's if you run it, and
everyone's if you run the company.**

Pure functions over already-loaded facts, so every case here can be tested
without a database and so the same answer is given however the request arrived
— the page, the API, or the assistant asking on somebody's behalf. That last
one is why this file exists at all rather than the checks living inline in the
router: the assistant reaches reports through the same routes, and a rule that
were written twice would eventually be two rules.

What a normal person can learn about somebody else's report is nothing: not its
contents, not its metrics, not that it exists. A report names customers, prices
and what somebody is stuck on, and the person who wrote it did so expecting
their manager to read it and not the whole company.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

from app.models.report import Report, ReportStatus
from app.roles.catalogue import SUPER_ADMIN

#: Global roles that see every team's reports. The CEO and managers run the
#: company; a super admin administers it and cannot be locked out of what they
#: are accountable for. Deliberately not ``FINANCE_ROLES``: an accountant reads
#: the accounts, which is a different question from reading what presales were
#: blocked on last Tuesday.
COMPANY_WIDE: Final[frozenset[str]] = frozenset({SUPER_ADMIN, "ceo", "manager"})

#: Team-scoped roles that see their own team's reports. These are held per team,
#: so holding one says nothing about any other team's reports — which is the
#: whole point of them being team-scoped.
TEAM_OVERSIGHT: Final[frozenset[str]] = frozenset({"team_manager", "team_lead"})


@dataclass(frozen=True, slots=True)
class Viewer:
    """The caller, as the reports module needs them.

    Gathered once per request. ``oversees`` is the set of teams where they hold
    a role that comes with reading other people's work — not every team they
    belong to, because being a member of presales does not make their
    colleagues' reports yours to read.
    """

    user_id: uuid.UUID
    #: Global role keys.
    roles: frozenset[str]
    #: Teams they belong to at all.
    team_ids: frozenset[uuid.UUID]
    #: Teams they run.
    oversees: frozenset[uuid.UUID]

    @property
    def is_company_wide(self) -> bool:
        return not COMPANY_WIDE.isdisjoint(self.roles)

    @property
    def is_super_admin(self) -> bool:
        return SUPER_ADMIN in self.roles


def may_read(report: Report, viewer: Viewer) -> bool:
    """Whether this person may read this report.

    A draft is its author's alone, whoever else is asking. Somebody's
    half-written notes read as a finished report is how people learn to write
    their reports somewhere else and paste them in at the end, and then the tool
    has bought nothing.
    """
    if report.author_id == viewer.user_id:
        return True
    if report.status != ReportStatus.SUBMITTED:
        return False
    if viewer.is_company_wide:
        return True
    return report.team_id in viewer.oversees


def may_comment(report: Report, viewer: Viewer) -> bool:
    """Only on a submitted report, and only if you can read it.

    The author is excluded on purpose. A comment is a reader's remark; an
    author with more to say has the remarks section, and if the report is
    already filed then what they have is a new report or a word with their
    manager. Letting them append would make "what did they report on Tuesday"
    an unanswerable question.
    """
    if report.status != ReportStatus.SUBMITTED:
        return False
    if report.author_id == viewer.user_id:
        return False
    return may_read(report, viewer)


def may_edit(report: Report, viewer: Viewer) -> bool:
    """Only the author, and only while it is a draft.

    Not even a super admin. Editing somebody else's account of their own week
    is not an administrative act, it is putting words in their mouth — and the
    record would still carry their name. A super admin who thinks a report is
    wrong comments on it, or deletes it and asks for another.
    """
    return report.author_id == viewer.user_id and report.status == ReportStatus.DRAFT


def may_delete(report: Report, viewer: Viewer) -> bool:
    """The author while it is a draft, or a super admin at any point.

    The super admin case is for the mistake that has to be undoable — a report
    filed against the wrong team, or one that should never have been submitted.
    It is deliberately not given to a CEO or manager: reading everything and
    being able to remove it are different powers, and only one of them needs to
    come with the job.
    """
    if viewer.is_super_admin:
        return True
    return may_edit(report, viewer)


def may_file_for(team_id: uuid.UUID, viewer: Viewer) -> bool:
    """Whether this person may file a report for this team.

    Membership, not oversight: filing a report is what an ordinary member does.
    A super admin may file anywhere, which is what makes the feature testable
    against a real team without first joining it.
    """
    return viewer.is_super_admin or team_id in viewer.team_ids


def readable_team_ids(viewer: Viewer) -> frozenset[uuid.UUID] | None:
    """Teams whose reports this person may read, or ``None`` for all of them.

    ``None`` rather than "every id" so a query can leave the clause off
    entirely, and so a new team does not quietly become invisible to the CEO
    because a set was built before it existed.
    """
    if viewer.is_company_wide:
        return None
    return viewer.oversees
