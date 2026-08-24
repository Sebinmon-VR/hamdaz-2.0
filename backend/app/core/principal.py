"""The resolved caller: who they are and exactly what they may do.

A :class:`Principal` is built once per request from the database and is immutable
thereafter. Authorization decisions read this object and nothing else — no global
``SUPERUSERS`` list refreshed by a background thread, which is root cause #2.

The scope rules, stated once so the rest of the codebase can stop thinking about them:

* An **org grant** (from a role with ``is_team_scoped=False``) applies everywhere.
* A **team grant** applies only inside that team, so it is consulted only when the caller
  names a team.
* ``all`` satisfies ``team`` satisfies ``own``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from app.core.rbac import Scope, get_permission


@dataclass(frozen=True, slots=True)
class TeamGrant:
    """What a user may do inside one team."""

    team_id: uuid.UUID
    team_slug: str
    role_key: str
    #: permission key → the widest scope granted in this team
    permissions: Mapping[str, Scope] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: uuid.UUID
    email: str
    display_name: str
    is_super_admin: bool = False
    #: Grants from org-scoped roles. These apply regardless of team.
    org_permissions: Mapping[str, Scope] = field(default_factory=dict)
    teams: Mapping[uuid.UUID, TeamGrant] = field(default_factory=dict)
    #: Active label keys per team, plus org-wide ones under ``None``. Read by the rules engine.
    labels: Mapping[uuid.UUID | None, frozenset[str]] = field(default_factory=dict)

    # ── permission resolution ──────────────────────────────────────────

    def effective_scope(
        self, permission_key: str, *, team_id: uuid.UUID | None = None
    ) -> Scope | None:
        """The widest scope this principal holds for a permission, or None.

        Validates the key against the registry so a typo fails loudly instead of silently
        denying access forever.
        """
        get_permission(permission_key)  # raises UnknownPermissionError on a typo

        if self.is_super_admin:
            return Scope.ALL

        best = self.org_permissions.get(permission_key)

        if team_id is not None:
            grant = self.teams.get(team_id)
            if grant is not None:
                team_scope = grant.permissions.get(permission_key)
                if team_scope is not None and (best is None or team_scope.rank > best.rank):
                    best = team_scope

        return best

    def has(
        self,
        permission_key: str,
        required: Scope = Scope.OWN,
        *,
        team_id: uuid.UUID | None = None,
    ) -> bool:
        granted = self.effective_scope(permission_key, team_id=team_id)
        return granted is not None and granted.satisfies(required)

    def teams_with(
        self, permission_key: str, required: Scope = Scope.TEAM
    ) -> frozenset[uuid.UUID]:
        """Every team where this principal holds the permission at ``required`` or wider.

        This is what team-scoped list queries filter on, so a router cannot accidentally
        return another team's rows.
        """
        if self.is_super_admin:
            return frozenset(self.teams)

        org_scope = self.org_permissions.get(permission_key)
        if org_scope is not None and org_scope.satisfies(Scope.ALL):
            return frozenset(self.teams)

        return frozenset(
            team_id
            for team_id, grant in self.teams.items()
            if (s := grant.permissions.get(permission_key)) is not None and s.satisfies(required)
        )

    def is_member_of(self, team_id: uuid.UUID) -> bool:
        return team_id in self.teams

    def label_keys(self, team_id: uuid.UUID | None = None) -> frozenset[str]:
        """Active labels for a team, unioned with org-wide labels."""
        org = self.labels.get(None, frozenset())
        if team_id is None:
            return org
        return org | self.labels.get(team_id, frozenset())

    def has_label(self, key: str, *, team_id: uuid.UUID | None = None) -> bool:
        return key in self.label_keys(team_id)

    # ── construction helper ────────────────────────────────────────────

    @staticmethod
    def merge_scopes(pairs: Iterable[tuple[str, Scope]]) -> dict[str, Scope]:
        """Collapse ``(key, scope)`` pairs, keeping the widest scope per key."""
        out: dict[str, Scope] = {}
        for key, scope in pairs:
            current = out.get(key)
            if current is None or scope.rank > current.rank:
                out[key] = scope
        return out


#: Used by unauthenticated endpoints that still want a uniform interface.
ANONYMOUS = Principal(
    user_id=uuid.UUID(int=0),
    email="",
    display_name="anonymous",
)
