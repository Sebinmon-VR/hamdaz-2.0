"""The new admin surfaces, described as data so a frontend can render them.

Scoped deliberately to what was built recently — the mail intake with its
Proposals mirror and live ranking, notifications, and team reports. The older
admin areas (roles, access, templates, leave, the assistant) already have their
own screens and are left alone; adding them here would be a second index of
things that are not lost.

This is a catalogue rather than a generated route dump because the useful thing
is not "what endpoints exist" — the OpenAPI schema says that — but what can be
configured, who may do it, and what it currently says. The status figures
beside each section are the ones that make a tile worth looking at: whether the
thing is on, and the one number that would worry somebody.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, Literal

#: Who a surface is for. ``super_admin`` is the narrow one — configuring what
#: the platform does on everybody's behalf — and is deliberately not the same
#: as the admin role that runs teams and people.
Audience = Literal["super_admin", "admin", "everyone"]


@dataclass(frozen=True, slots=True)
class Endpoint:
    method: str
    path: str
    #: What it does, for somebody building the screen rather than calling it.
    what: str
    #: True for anything that changes data, so a frontend can style it and a
    #: reader can see how much of a section is read-only.
    writes: bool = False


@dataclass(frozen=True, slots=True)
class Section:
    key: str
    name: str
    audience: Audience
    description: str
    #: The thing worth knowing before opening it — usually the risk.
    caution: str | None = None
    endpoints: tuple[Endpoint, ...] = field(default_factory=tuple)


_PREFIX: Final = "/api/v1"


def _e(method: str, path: str, what: str, *, writes: bool = False) -> Endpoint:
    return Endpoint(method, f"{_PREFIX}{path}", what, writes)


SECTIONS: Final[tuple[Section, ...]] = (
    Section(
        "intake",
        "Mail intake",
        "super_admin",
        "Watches a mailbox, decides what each message is, looks for it in the "
        "Proposals list, and either raises a task or tells whoever holds it.",
        caution=(
            "create_in_sharepoint ships off. Turning it on is the moment this "
            "starts writing rows into the live Proposals list the team works "
            "in — the only thing here that cannot be undone. update_negotiation "
            "and update_order_status each set one column on a task that already "
            "exists; both ship off too."
        ),
        endpoints=(
            _e("GET", "/intake/settings", "What is watched, and what it may do"),
            _e("PATCH", "/intake/settings", "Change it", writes=True),
            _e("GET", "/intake/messages", "Every mail seen, including the ignored"),
            _e("GET", "/intake/messages/{id}", "One mail: reasoning, shortlist, payload"),
            _e("POST", "/intake/messages/{id}/retry", "Run one through again", writes=True),
            _e("POST", "/intake/subscription", "Ask Graph to notify us", writes=True),
        ),
    ),
    Section(
        "mirror",
        "Proposals mirror",
        "super_admin",
        "The local copy of the Proposals list. Every question about that list — "
        "matching an email to a task, counting somebody's workload — is "
        "answered from here rather than from SharePoint, which is what keeps "
        "both fast however large the list grows.",
        caution="Reads SharePoint on a timer. Writes nothing to it, ever.",
        endpoints=(
            _e("GET", "/intake/mirror", "Row count, embedding coverage, last sync"),
            _e("POST", "/intake/mirror/sync", "Refresh it now", writes=True),
        ),
    ),
    Section(
        "standing",
        "Who gets the next task",
        "super_admin",
        "The live priority ranking, recomputed whenever the mirror changes "
        "rather than when somebody asks. Rank 1 is who the intake will assign "
        "the next new tender to.",
        endpoints=(
            _e("GET", "/intake/standing", "The current ranking, and why"),
            _e("GET", "/analytics/runs", "Saved rankings, for past decisions"),
        ),
    ),
    Section(
        "reports",
        "Team reports",
        "super_admin",
        "What each team files and how often, which template they file against, "
        "who a filed report is emailed to, and a log of what was sent.",
        endpoints=(
            _e("GET", "/reports/admin/settings", "Who filed reports are sent to"),
            _e("PATCH", "/reports/admin/settings", "Change it", writes=True),
            _e("GET", "/reports/admin/deliveries", "What was emailed, and what failed"),
            _e("GET", "/reports/admin/templates", "Report templates to choose from"),
            _e("GET", "/reports/admin/schedules", "Which template each team files"),
            _e("PUT", "/reports/admin/schedules", "Point a team at a template", writes=True),
        ),
    ),
    Section(
        "notifications",
        "Notifications",
        "everyone",
        "What each person has been told, in the app and in Teams. Everyone sees "
        "their own and nobody else's — there is deliberately no route that "
        "takes a user id.",
        endpoints=(
            _e("GET", "/notifications", "My notifications"),
            _e("GET", "/notifications/unread-count", "The number on the bell"),
            _e("POST", "/notifications/read", "Mark as read", writes=True),
        ),
    ),
)

SECTIONS_BY_KEY: Final[dict[str, Section]] = {s.key: s for s in SECTIONS}


# ── who may do what, in the parts just built ───────────────────────────


@dataclass(frozen=True, slots=True)
class Rule:
    """One permission rule, in words a screen can print.

    These live in code — in ``app.reports.access``, in the intake router, in
    the role catalogue — and a frontend cannot derive them from any endpoint.
    Restating them here is the only way an admin screen can explain to somebody
    why they are being refused, so the wording is the product rather than a
    comment about it.
    """

    area: str
    what: str
    #: Role keys, or a phrase where the answer is not a role at all.
    who: tuple[str, ...] = ()
    note: str | None = None


PERMISSION_RULES: Final[tuple[Rule, ...]] = (
    Rule(
        "intake", "Configure the mail intake and read its log",
        ("super_admin",),
        "The log holds who was told what about whose work, and the settings "
        "decide whose mailbox is read. Narrower than the admin role on "
        "purpose — a CEO cannot open it either.",
    ),
    Rule(
        "intake", "Turn on writing to the live Proposals list",
        ("super_admin",),
        "The one irreversible switch. Off, a tender records the payload it "
        "would post and posts nothing.",
    ),
    Rule(
        "mirror", "Refresh the local copy of the Proposals list",
        ("super_admin",),
        "Reads SharePoint. Nothing here writes to it.",
    ),
    Rule(
        "standing", "See the live ranking and why somebody is placed there",
        ("super_admin",),
        "Who is next carries everybody's workload and any exclusion reason, "
        "which is more about a colleague than a colleague should read.",
    ),
    Rule(
        "reports", "File a report",
        (),
        "Anyone on a team that has been granted the Reports module. Filing is "
        "ordinary work, not administration.",
    ),
    Rule(
        "reports", "Read somebody else's submitted report",
        ("team_manager", "team_lead", "manager", "ceo", "super_admin"),
        "The team roles see only their own team's. A colleague on the same "
        "team sees none of yours — being on presales does not make a "
        "teammate's report yours to read.",
    ),
    Rule(
        "reports", "Read a draft",
        (),
        "Its author, and nobody else — not a CEO, not a super admin. "
        "Half-written notes read as a finished report is how people learn to "
        "draft somewhere else.",
    ),
    Rule(
        "reports", "Edit or submit a report",
        (),
        "Its author, and only before it is submitted. Not even a super admin: "
        "editing somebody's account of their own week is putting words in "
        "their mouth, and the record would still carry their name.",
    ),
    Rule(
        "reports", "Delete a submitted report",
        ("super_admin",),
        "Reading everything and being able to remove it are different powers, "
        "so this is deliberately not given to a CEO or a manager.",
    ),
    Rule(
        "reports", "Decide what each team is asked to report",
        ("super_admin",),
        "What the business records is not something a team quietly changes "
        "for itself.",
    ),
    Rule(
        "notifications", "Read notifications",
        (),
        "Your own, always and only. No route accepts a user id.",
    ),
)
