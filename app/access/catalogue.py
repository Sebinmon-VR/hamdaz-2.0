"""What the ERP is made of: the modules a team can be given, and their pages.

Modules are code, not user data, so the authoritative list lives here and is
seeded into the database. The table exists so grants can carry a real foreign key
and so an admin screen can join against it — the same arrangement as roles.

Adding a module means adding one entry here and running the seed. Nothing else
needs to know.

Two kinds of module, told apart by ``admin_only``:

* ordinary modules are granted per team, and a team sees nothing it was not given
* ``admin_only`` modules (roles, user administration) are never granted to a
  team. Reaching them depends on holding a global admin role, which is enforced
  by those endpoints themselves. Letting a team be "given" one would imply an
  access route that does not exist.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final


@dataclass(frozen=True, slots=True)
class PageSpec:
    #: Unique within its module.
    key: str
    name: str
    #: The frontend route. The backend never serves it; it is here so one
    #: catalogue drives both the permission model and the navigation.
    path: str
    #: Rendered inside a team's context — its path carries [slug] and its data
    #: is that team's. The personal equivalent, if any, is a separate page.
    team_scoped: bool = False


@dataclass(frozen=True, slots=True)
class ModuleSpec:
    key: str
    name: str
    description: str
    pages: tuple[PageSpec, ...] = field(default_factory=tuple)
    #: Gated by a global admin role rather than by team grants.
    admin_only: bool = False


MODULES: Final[tuple[ModuleSpec, ...]] = (
    ModuleSpec(
        key="dashboard",
        name="Dashboard",
        description="Landing page and summary of what a person has to do.",
        pages=(
            PageSpec("overview", "My overview", "/dashboard"),
            PageSpec("team", "Team dashboard", "/teams/[slug]/dashboard", team_scoped=True),
        ),
    ),
    ModuleSpec(
        key="directory",
        name="People Directory",
        description="Everyone in the organisation, read from Entra.",
        pages=(
            PageSpec("list", "All people", "/directory"),
            PageSpec("detail", "Person detail", "/directory/[id]"),
        ),
    ),
    ModuleSpec(
        key="teams",
        name="Teams",
        description="Teams and who belongs to them.",
        pages=(
            PageSpec("list", "All teams", "/teams"),
            PageSpec("detail", "Team detail", "/teams/[slug]", team_scoped=True),
            PageSpec("members", "Manage members", "/teams/[slug]/members", team_scoped=True),
        ),
    ),
    ModuleSpec(
        key="leave",
        name="Leave",
        description=(
            "Request time off. Open to everyone; the HR team decides. Listed here "
            "for navigation — it is not gated by team grants."
        ),
        pages=(
            PageSpec("mine", "My leave", "/leave"),
            PageSpec("request", "Request leave", "/leave/request"),
            PageSpec("calendar", "Who is off", "/leave/calendar"),
            PageSpec("queue", "Requests to decide", "/leave/requests"),
            PageSpec("rules", "Leave rules", "/leave/settings"),
        ),
    ),
    ModuleSpec(
        key="proposals",
        name="Proposals",
        description="Proposal tasks from the SharePoint Proposals list.",
        pages=(
            PageSpec("my_tasks", "My tasks", "/proposals/my-tasks"),
            PageSpec(
                "team_tasks", "Team tasks", "/teams/[slug]/proposals", team_scoped=True
            ),
        ),
    ),
    ModuleSpec(
        key="projects",
        name="Projects",
        description=(
            "Project management for a team: the plan, its milestones, the tasks "
            "people hold, the issues in the way, and a log of every movement. "
            "Members see the projects they are on and move their own work; team "
            "leads and managers run the plan; the CEO and super admins see the "
            "whole portfolio. Who sees which project is decided in "
            "app/projects/access.py and not by this grant, which only decides "
            "who can reach the module at all."
        ),
        pages=(
            PageSpec("board", "My work", "/projects/board"),
            PageSpec("list", "All projects", "/projects"),
            PageSpec("detail", "Project", "/projects/[id]"),
            PageSpec("plan", "Plan and milestones", "/projects/[id]/plan"),
            PageSpec("portfolio", "Portfolio", "/projects/portfolio"),
            PageSpec("activity", "What moved", "/projects/activity"),
            PageSpec("team", "Team projects", "/teams/[slug]/projects", team_scoped=True),
        ),
    ),
    ModuleSpec(
        key="reports",
        name="Reports",
        description=(
            "What each team files — daily, weekly or monthly — and what those "
            "reports say together. Everyone files their own; managers and leads "
            "read their team's; the CEO and super admins read all of them. Who "
            "sees whose is decided in app/reports/access.py and not by this "
            "grant, which only decides who can reach the module at all."
        ),
        pages=(
            PageSpec("mine", "My reports", "/reports"),
            PageSpec("new", "File a report", "/reports/new"),
            PageSpec("detail", "Report", "/reports/[id]"),
            PageSpec("team", "Team reports", "/teams/[slug]/reports", team_scoped=True),
            PageSpec("overview", "Reporting overview", "/reports/overview"),
        ),
    ),
    ModuleSpec(
        key="quotes",
        name="Quotes",
        description=(
            "Quotes read from Zoho Books, with the customer, items, sales orders "
            "and comments attached to each. Read-only. Listed here for navigation "
            "— like leave, it is open to everyone rather than gated by team grants."
        ),
        pages=(
            PageSpec("list", "All quotes", "/quotes"),
            PageSpec("detail", "Quote detail", "/quotes/[id]"),
        ),
    ),
    ModuleSpec(
        key="quote_comparison",
        name="Quote Comparison",
        description=(
            "Upload supplier quotes, extract them, and compare like with like. "
            "Granted to presales rather than the whole company: these are live "
            "bid prices."
        ),
        pages=(
            PageSpec("list", "Comparisons", "/comparisons"),
            PageSpec("new", "New comparison", "/comparisons/new"),
            PageSpec("detail", "Comparison", "/comparisons/[id]"),
        ),
    ),
    ModuleSpec(
        key="quote_requests",
        name="Quote Requests",
        description=(
            "Raise a customer quote, attach the supplier quotes behind it, and "
            "take it through approval. Approved ones queue for creation in Zoho "
            "— which nothing here does yet."
        ),
        pages=(
            PageSpec("list", "Quote requests", "/quote-requests"),
            PageSpec("new", "New quote", "/quote-requests/new"),
            PageSpec("detail", "Quote", "/quote-requests/[id]"),
            PageSpec("queue", "Ready for Zoho", "/quote-requests/queue"),
        ),
    ),
    ModuleSpec(
        key="assignment",
        name="Work Assignment",
        description=(
            "User labels and the policy that decides how work is shared out — "
            "capacity ratios, limits and who is in the pool. Listed for "
            "navigation; who may EDIT a policy is decided by role and team "
            "membership, not by a team grant."
        ),
        pages=(
            PageSpec("labels", "Labels", "/assignment/labels"),
            PageSpec("policy", "Assignment policy", "/assignment/policy"),
            PageSpec("preview", "Who gets what", "/assignment/preview"),
        ),
    ),
    ModuleSpec(
        key="hr",
        name="HR",
        description=(
            "Hiring, employee documents and performance reviews. Listed here for "
            "navigation; who may actually use it is membership of the HR team, "
            "which the leave settings name, not a team grant. The candidate side "
            "is not a page here at all — it is a public link with no route back."
        ),
        pages=(
            PageSpec("openings", "Job openings", "/hr/openings"),
            PageSpec("opening", "Opening", "/hr/openings/[id]"),
            PageSpec("applications", "Applications", "/hr/applications"),
            PageSpec("application", "Candidate", "/hr/applications/[id]"),
            PageSpec("people", "Staff documents", "/hr/people"),
            PageSpec("person", "Person", "/hr/people/[id]"),
            PageSpec("cycles", "Review cycles", "/hr/reviews"),
            PageSpec("cycle", "Review cycle", "/hr/reviews/[id]"),
            PageSpec("my_reviews", "Reviews to write", "/hr/my-reviews"),
            PageSpec("my_record", "My HR record", "/hr/me"),
        ),
    ),
    ModuleSpec(
        key="finance",
        name="Finance",
        description=(
            "Profit and loss computed from the Zoho Books ledger, and the ledger "
            "data behind it. Gated by a global role — super admin, CEO, Manager "
            "or Accountant — rather than by a team grant, because there is one "
            "set of company accounts and not a set per team. admin_only here "
            "means 'not grantable to a team', which is exactly right: there is "
            "no team that should be given the company P&L."
        ),
        admin_only=True,
        pages=(
            PageSpec("statement", "Profit & loss", "/finance/profit-and-loss"),
            PageSpec("comparison", "Period comparison", "/finance/comparison"),
            PageSpec("trend", "Monthly trend", "/finance/trend"),
            PageSpec("account", "Account detail", "/finance/accounts/[id]"),
            PageSpec("diagnostics", "Zoho data health", "/finance/diagnostics"),
        ),
    ),
    ModuleSpec(
        key="assistant",
        name="Assistant",
        description=(
            "A chat assistant over every module, acting as the person asking. "
            "Listed here for navigation; who may use it is decided by the "
            "assistant's own access rules, which a super admin sets, not by a "
            "team grant."
        ),
        pages=(PageSpec("chat", "Assistant", "/assistant"),),
    ),
    ModuleSpec(
        key="assistant_admin",
        name="Assistant Administration",
        description=(
            "The assistant's switches: model, what it may read and write per "
            "module, who it is released to, every run it has made and what "
            "they cost. Super admin only — narrower than admin_only usually "
            "means, and enforced by the endpoints themselves."
        ),
        admin_only=True,
        pages=(
            PageSpec("settings", "Assistant settings", "/admin/assistant"),
            PageSpec("policies", "Permissions", "/admin/assistant/permissions"),
            PageSpec("rules", "Access rules", "/admin/assistant/access"),
            PageSpec("runs", "Runs & logs", "/admin/assistant/runs"),
            PageSpec("analytics", "Usage & cost", "/admin/assistant/analytics"),
        ),
    ),
    ModuleSpec(
        key="templates",
        name="Form Templates",
        description=(
            "What the forms ask for, as data rather than code. Only a super "
            "admin creates or changes one; who may use each is set per team and "
            "per role."
        ),
        admin_only=True,
        pages=(
            PageSpec("list", "Templates", "/admin/templates"),
            PageSpec("edit", "Edit template", "/admin/templates/[key]"),
        ),
    ),
    ModuleSpec(
        key="roles",
        name="Roles & Permissions",
        description="The role catalogue and who holds what.",
        admin_only=True,
        pages=(
            PageSpec("catalogue", "Roles", "/admin/roles"),
            PageSpec("assignments", "Assignments", "/admin/roles/assignments"),
        ),
    ),
    ModuleSpec(
        key="user_admin",
        name="User Administration",
        description="Full user profiles, resetting and removing accounts.",
        admin_only=True,
        pages=(
            PageSpec("profile", "User profile", "/admin/users/[id]"),
            PageSpec("access", "Team module access", "/admin/access"),
        ),
    ),
)

BY_KEY: Final[dict[str, ModuleSpec]] = {m.key: m for m in MODULES}

#: Modules a team can actually be granted.
GRANTABLE: Final[tuple[ModuleSpec, ...]] = tuple(m for m in MODULES if not m.admin_only)

#: Only a super admin may change team visibility — straight from the brief.
ACCESS_ADMINS: Final[frozenset[str]] = frozenset({"super_admin"})
