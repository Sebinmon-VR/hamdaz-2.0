"""The permission registry — one source of truth for authorization.

Root cause #1 in the legacy audit is that roles lived in a OneDrive spreadsheet and were
dispatched through an ``if/elif`` chain. The fix is not "put roles in a table"; it is to make
permissions *declarative data* that a single registry owns.

That registry drives three things at once, so they cannot drift apart:

1. **Enforcement** — :func:`require` builds a FastAPI dependency from a permission key.
2. **The admin UI** — :func:`registry_by_module` is serialised to build the role editor's
   checkbox matrix. A permission that does not exist here cannot be granted.
3. **The docs** — the same structure generates the permission table in the project plan.

Adding a permission means adding one line here. Nothing else needs to know.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum


class Scope(StrEnum):
    """How wide a granted permission reaches.

    Ordered narrowest-first; :meth:`satisfies` relies on that ordering.
    """

    OWN = "own"
    TEAM = "team"
    ALL = "all"

    @property
    def rank(self) -> int:
        return _SCOPE_RANK[self]

    def satisfies(self, required: Scope) -> bool:
        """True when a grant at this scope covers a requirement at ``required``.

        ``all`` satisfies ``team`` satisfies ``own``.
        """
        return self.rank >= required.rank


_SCOPE_RANK: dict[Scope, int] = {Scope.OWN: 0, Scope.TEAM: 1, Scope.ALL: 2}


@dataclass(frozen=True, slots=True)
class Permission:
    key: str
    module: str
    description: str
    #: Scopes that are meaningful for this permission. A permission that is inherently
    #: org-wide (``admin.teams.manage``) offers only ``all``, so the admin UI will not
    #: render a nonsensical "own" option for it.
    scopes: tuple[Scope, ...] = (Scope.OWN, Scope.TEAM, Scope.ALL)

    def __post_init__(self) -> None:
        if not self.scopes:
            raise ValueError(f"permission {self.key!r} must allow at least one scope")


def _p(
    key: str,
    module: str,
    description: str,
    scopes: tuple[Scope, ...] = (Scope.OWN, Scope.TEAM, Scope.ALL),
) -> Permission:
    return Permission(key=key, module=module, description=description, scopes=scopes)


_ORG_ONLY = (Scope.ALL,)
_TEAM_UP = (Scope.TEAM, Scope.ALL)

# ──────────────────────────────────────────────────────────────────────────
# The registry. Mirrors docs/PROJECT_PLAN.md §4.3.
# ──────────────────────────────────────────────────────────────────────────
PERMISSIONS: tuple[Permission, ...] = (
    # ── proposals ──
    _p("proposals.read", "proposals", "View proposals"),
    _p("proposals.create", "proposals", "Create a proposal", _TEAM_UP),
    _p("proposals.update", "proposals", "Edit a proposal"),
    _p("proposals.delete", "proposals", "Delete a proposal", _TEAM_UP),
    _p("proposals.assign", "proposals", "Assign a proposal to a member", _TEAM_UP),
    _p("proposals.reassign", "proposals", "Override an existing assignment", _TEAM_UP),
    # ── quotes ──
    _p("quotes.read", "quotes", "View quotes"),
    _p("quotes.create", "quotes", "Create a quote", _TEAM_UP),
    _p("quotes.update", "quotes", "Edit a quote"),
    _p("quotes.submit", "quotes", "Submit a quote for approval"),
    _p("quotes.approve", "quotes", "Approve a submitted quote", _TEAM_UP),
    _p("quotes.reject", "quotes", "Reject a submitted quote", _TEAM_UP),
    _p("quotes.export", "quotes", "Export a quote to docx/xlsx"),
    # ── vendors & contacts ──
    _p("vendors.read", "vendors", "View vendors and distributors"),
    _p("vendors.create", "vendors", "Add a vendor", _TEAM_UP),
    _p("vendors.update", "vendors", "Edit a vendor", _TEAM_UP),
    _p("contacts.read", "contacts", "View contacts and customers"),
    _p("contacts.update", "contacts", "Edit a contact", _TEAM_UP),
    # ── leave ──
    _p("leave.request", "leave", "Request leave", (Scope.OWN,)),
    _p("leave.cancel", "leave", "Cancel a leave request"),
    _p("leave.read_own", "leave", "View own leave history", (Scope.OWN,)),
    _p("leave.read_team", "leave", "View the team's leave calendar", _TEAM_UP),
    _p("leave.approve", "leave", "Approve or reject leave", _TEAM_UP),
    _p("leave.configure", "leave", "Configure holidays and leave policy", _TEAM_UP),
    # ── mail ──
    _p("mail.read", "mail", "Read mailbox"),
    _p("mail.send", "mail", "Send mail"),
    _p("mail.delete", "mail", "Delete mail", (Scope.OWN,)),
    # ── reports ──
    _p("reports.read_own", "reports", "View own performance report", (Scope.OWN,)),
    _p("reports.read_team", "reports", "View team reports", _TEAM_UP),
    _p("reports.read_org", "reports", "View organisation-wide reports", _ORG_ONLY),
    # ── rules engine (§5.2) ──
    _p("rules.read", "rules", "View rule sets and policies"),
    _p("rules.edit_team", "rules", "Edit team-scoped rule sets", _TEAM_UP),
    _p("rules.edit_org", "rules", "Edit organisation-wide rule sets", _ORG_ONLY),
    _p("rules.simulate", "rules", "Dry-run a rule set against live data", _TEAM_UP),
    _p("rules.publish", "rules", "Publish a rule set version", _TEAM_UP),
    # ── labels (§5.3) ──
    _p("labels.read", "labels", "View user labels"),
    _p("labels.assign", "labels", "Assign labels to users", _TEAM_UP),
    _p("labels.manage", "labels", "Create and edit label definitions", _ORG_ONLY),
    # ── admin (§5.1) ──
    _p("admin.teams.manage", "admin", "Create, edit and archive teams", _ORG_ONLY),
    _p("admin.users.manage", "admin", "Invite, edit and deactivate users", _ORG_ONLY),
    _p("admin.roles.manage", "admin", "Create and edit roles", _ORG_ONLY),
    _p("admin.modules.configure", "admin", "Enable or disable modules", _ORG_ONLY),
    _p("admin.audit.read", "admin", "Read the audit log", _ORG_ONLY),
    # ── developer panel (§6) ──
    _p("dev.panel.view", "dev", "Access the developer panel", _ORG_ONLY),
    _p("dev.logs.read", "dev", "Read logs and traces", _ORG_ONLY),
    _p("dev.jobs.manage", "dev", "Run, pause, retry and replay jobs", _ORG_ONLY),
    _p("dev.tools.create", "dev", "Create and edit tools", _ORG_ONLY),
    _p("dev.connectors.configure", "dev", "Configure connectors", _ORG_ONLY),
    _p("dev.flags.manage", "dev", "Manage feature flags", _ORG_ONLY),
)

PERMISSIONS_BY_KEY: dict[str, Permission] = {p.key: p for p in PERMISSIONS}

if len(PERMISSIONS_BY_KEY) != len(PERMISSIONS):  # pragma: no cover - import-time guard
    raise RuntimeError("duplicate permission key in registry")


def registry_by_module() -> dict[str, list[Permission]]:
    """Grouped view, used by the admin role editor and the generated docs."""
    grouped: defaultdict[str, list[Permission]] = defaultdict(list)
    for permission in PERMISSIONS:
        grouped[permission.module].append(permission)
    return dict(grouped)


def get_permission(key: str) -> Permission:
    try:
        return PERMISSIONS_BY_KEY[key]
    except KeyError:
        raise UnknownPermissionError(key) from None


class UnknownPermissionError(KeyError):
    """Raised when code references a permission that is not in the registry.

    This is a programming error, not a runtime authorization failure — it fires at import
    or wiring time so a typo cannot silently become an endpoint nobody can reach.
    """

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key

    def __str__(self) -> str:
        return f"unknown permission {self.key!r} — add it to app.core.rbac.PERMISSIONS"


# ──────────────────────────────────────────────────────────────────────────
# Built-in roles (§4.2). Admins compose custom roles from the registry above;
# these ship with the product and cannot be deleted.
# ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SystemRole:
    key: str
    name: str
    description: str
    #: ``(permission_key, scope)`` pairs. ``"*"`` means every permission in the registry.
    grants: tuple[tuple[str, Scope], ...]
    is_team_scoped: bool


def _effective_scope(permission: Permission, wanted: Scope) -> Scope:
    """Clamp ``wanted`` to the widest scope this permission actually supports.

    Some permissions are inherently personal — ``leave.request`` is only ever ``own``. Asking
    for ``all`` there is not an error; it means "as wide as this goes". Dropping the grant
    instead would silently leave a hole, which is exactly the bug the registry tests catch.
    """
    if wanted in permission.scopes:
        return wanted
    return max(permission.scopes, key=lambda s: s.rank)


def _grant_all(scope: Scope) -> tuple[tuple[str, Scope], ...]:
    """Grant every permission in the registry, clamped per permission."""
    return tuple((p.key, _effective_scope(p, scope)) for p in PERMISSIONS)


def _grant(module_or_keys: tuple[str, ...], scope: Scope) -> tuple[tuple[str, Scope], ...]:
    """Grant every permission whose key starts with one of the given prefixes."""
    return tuple(
        (p.key, _effective_scope(p, scope))
        for p in PERMISSIONS
        if p.key.startswith(module_or_keys)
    )


SYSTEM_ROLES: tuple[SystemRole, ...] = (
    SystemRole(
        key="super_admin",
        name="Super Admin",
        description="Full access, including the admin and developer panels.",
        grants=_grant_all(Scope.ALL),
        is_team_scoped=False,
    ),
    SystemRole(
        key="developer",
        name="Developer",
        description=(
            "Developer panel, tool builder, connectors and logs. Deliberately holds no "
            "business-data write permission."
        ),
        grants=(
            *_grant(("dev.",), Scope.ALL),
            *_grant(("rules.read", "labels.read"), Scope.ALL),
            ("admin.audit.read", Scope.ALL),
            ("proposals.read", Scope.ALL),
            ("quotes.read", Scope.ALL),
        ),
        is_team_scoped=False,
    ),
    SystemRole(
        key="org_manager",
        name="Organisation Manager",
        description="Cross-team reports, approvals and leave oversight.",
        grants=(
            *_grant(
                ("proposals.", "quotes.", "vendors.", "contacts.", "leave.", "reports."),
                Scope.ALL,
            ),
            *_grant(("rules.read", "rules.simulate", "labels.read"), Scope.ALL),
        ),
        is_team_scoped=False,
    ),
    SystemRole(
        key="team_manager",
        name="Team Manager",
        description="Manage the team's members, assignments and approvals; edit team rules.",
        grants=(
            *_grant(("proposals.", "quotes.", "vendors.", "contacts."), Scope.TEAM),
            *_grant(("leave.",), Scope.TEAM),
            *_grant(("mail.",), Scope.OWN),
            ("reports.read_own", Scope.OWN),
            ("reports.read_team", Scope.TEAM),
            ("rules.read", Scope.TEAM),
            ("rules.edit_team", Scope.TEAM),
            ("rules.simulate", Scope.TEAM),
            ("rules.publish", Scope.TEAM),
            ("labels.read", Scope.TEAM),
            ("labels.assign", Scope.TEAM),
        ),
        is_team_scoped=True,
    ),
    SystemRole(
        key="team_member",
        name="Team Member",
        description="Do the work: proposals, quotes, leave requests.",
        grants=(
            ("proposals.read", Scope.TEAM),
            ("proposals.create", Scope.TEAM),
            ("proposals.update", Scope.OWN),
            ("quotes.read", Scope.TEAM),
            ("quotes.create", Scope.TEAM),
            ("quotes.update", Scope.OWN),
            ("quotes.submit", Scope.OWN),
            ("quotes.export", Scope.TEAM),
            ("vendors.read", Scope.TEAM),
            ("contacts.read", Scope.TEAM),
            ("leave.request", Scope.OWN),
            ("leave.cancel", Scope.OWN),
            ("leave.read_own", Scope.OWN),
            ("leave.read_team", Scope.TEAM),
            ("mail.read", Scope.OWN),
            ("mail.send", Scope.OWN),
            ("mail.delete", Scope.OWN),
            ("reports.read_own", Scope.OWN),
            ("labels.read", Scope.TEAM),
        ),
        is_team_scoped=True,
    ),
    SystemRole(
        key="viewer",
        name="Viewer",
        description="Read-only access to the team's work.",
        grants=(
            ("proposals.read", Scope.TEAM),
            ("quotes.read", Scope.TEAM),
            ("vendors.read", Scope.TEAM),
            ("contacts.read", Scope.TEAM),
            ("leave.read_team", Scope.TEAM),
        ),
        is_team_scoped=True,
    ),
)

SYSTEM_ROLES_BY_KEY: dict[str, SystemRole] = {r.key: r for r in SYSTEM_ROLES}


def validate_registry() -> None:
    """Assert every role grant references a real permission at a supported scope.

    Called at import time by the test suite and by the Alembic seed, so a bad grant is a
    build failure rather than a mystery 403 in production.
    """
    for role in SYSTEM_ROLES:
        for key, scope in role.grants:
            permission = get_permission(key)  # raises UnknownPermissionError
            if scope not in permission.scopes:
                raise ValueError(
                    f"role {role.key!r} grants {key!r} at scope {scope!r}, "
                    f"but that permission supports only {[s.value for s in permission.scopes]}"
                )
