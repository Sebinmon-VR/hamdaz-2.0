"""The roles the platform ships with, and which of them confer admin authority.

This is the one place the six system roles are defined. The seeder writes them
into the ``roles`` table; the guards read ``ADMIN_ROLES`` from here. Adding a
seventh system role means adding one line.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from app.models.role import RoleScope


@dataclass(frozen=True, slots=True)
class SystemRole:
    key: str
    name: str
    scope: RoleScope
    description: str


SYSTEM_ROLES: Final[tuple[SystemRole, ...]] = (
    SystemRole(
        key="super_admin",
        name="Super Admin",
        scope=RoleScope.GLOBAL,
        description="Full control of the platform, including granting super admin.",
    ),
    SystemRole(
        key="ceo",
        name="CEO",
        scope=RoleScope.GLOBAL,
        description="Organisation-wide oversight. Can manage teams, roles and people.",
    ),
    SystemRole(
        key="manager",
        name="Manager",
        scope=RoleScope.GLOBAL,
        description="Manages teams and their members across the organisation.",
    ),
    SystemRole(
        key="accountant",
        name="Accountant",
        scope=RoleScope.GLOBAL,
        description=(
            "Reads the company accounts: profit and loss, and the Zoho Books "
            "ledger behind it. Global rather than per team — there is one set of "
            "company accounts, not a set per team. Deliberately NOT an admin "
            "role: see ADMIN_ROLES below for why finance and user administration "
            "are kept apart."
        ),
    ),
    SystemRole(
        key="quote_approver",
        name="Quote Approver",
        scope=RoleScope.GLOBAL,
        description=(
            "A named approver for customer quotes, whatever team they are in: "
            "sees every quote waiting for approval, decides it, and is emailed "
            "when one is sent. Opens the Quote Requests list and quotes only — "
            "not drafts or the archive. Given from the quote approval settings."
        ),
    ),
    SystemRole(
        key="team_manager",
        name="Team Manager",
        scope=RoleScope.TEAM,
        description=(
            "Manages one team. Held per team, unlike the organisation-wide "
            "Manager role: it lets somebody set that team's assignment policy "
            "without any reach over other teams. Managers are not given work by "
            "the assignment scoring."
        ),
    ),
    SystemRole(
        key="team_lead",
        name="Team Lead",
        scope=RoleScope.TEAM,
        description="Leads one team. Held per team, not organisation-wide.",
    ),
    SystemRole(
        key="member",
        name="Member",
        scope=RoleScope.TEAM,
        description="Belongs to a team.",
    ),
    SystemRole(
        key="approver",
        name="Approver",
        scope=RoleScope.TEAM,
        description="Approves work within a team.",
    ),
)

SUPER_ADMIN: Final = "super_admin"
TEAM_LEAD: Final = "team_lead"
QUOTE_APPROVER: Final = "quote_approver"

#: Modules a global role opens by itself, without the holder's team having
#: been granted them, and which pages. ``None`` would mean every page.
#:
#: Narrow on purpose. A named quote approver is somebody outside the teams
#: that quote (Accounts, say) asked to sign off prices; they need the queue and
#: the quote, not the screen for raising one or the Zoho queue.
ROLE_MODULES: Final[dict[str, dict[str, tuple[str, ...] | None]]] = {
    QUOTE_APPROVER: {"quote_requests": ("list", "detail")},
    # A customer writing in through the website is the business's concern
    # before it is any team's: whoever runs the company sees it without
    # waiting for a grant. Sales is given it as a team.
    "ceo": {"website_enquiries": None},
    "manager": {"website_enquiries": None},
}

#: What someone gets when added to a team without a role being named.
DEFAULT_TEAM_ROLE: Final = "member"

#: Who may create teams, create roles, and grant roles. Straight from the brief:
#: super admin, CEO and managers.
ADMIN_ROLES: Final[frozenset[str]] = frozenset({SUPER_ADMIN, "ceo", "manager"})

#: Who may read the company accounts — the profit and loss and the Zoho Books
#: ledger behind it.
#:
#: A separate set rather than a reuse of ADMIN_ROLES, and the distinction is the
#: point. ADMIN_ROLES answers "who may administer people", which is a different
#: question from "who may see company profit". Collapsing the two would mean the
#: Accounts team could only be given the P&L by being made a Manager, which
#: would also hand them the power to create teams and grant roles — a privilege
#: nobody asked for, arriving as a side effect of a reporting requirement. That
#: is the ordinary way an access model quietly stops meaning anything.
#:
#: The reverse also holds: ``accountant`` confers no administrative authority at
#: all. It reads the accounts and nothing else.
FINANCE_ROLES: Final[frozenset[str]] = frozenset(
    {SUPER_ADMIN, "ceo", "manager", "accountant"}
)

#: Granting or revoking super_admin requires *being* super_admin — a manager
#: who could grant it to themselves would make the distinction meaningless, and
#: that is the standard way a privilege model quietly collapses. This is
#: deliberately narrower than "admins may assign roles"; see the module README
#: note if you want managers to be able to mint super admins.
SUPER_ADMIN_GRANTORS: Final[frozenset[str]] = frozenset({SUPER_ADMIN})

#: The account bootstrapped as super admin so the system has a first
#: administrator. Everything after that is granted through the API.
BOOTSTRAP_SUPER_ADMIN_EMAIL: Final = "sebin@hamdaz.com"
