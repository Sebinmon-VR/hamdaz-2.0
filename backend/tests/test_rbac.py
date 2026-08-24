"""The permission registry and scope algebra.

These run without a database, and they are the tests that matter most: if scope resolution is
wrong, every endpoint in the system is wrong in the same way.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.principal import Principal, TeamGrant
from app.core.rbac import (
    PERMISSIONS,
    PERMISSIONS_BY_KEY,
    SYSTEM_ROLES,
    SYSTEM_ROLES_BY_KEY,
    Scope,
    UnknownPermissionError,
    get_permission,
    registry_by_module,
    validate_registry,
)


class TestRegistryIntegrity:
    def test_registry_is_internally_consistent(self) -> None:
        """Every system-role grant names a real permission at a supported scope."""
        validate_registry()

    def test_no_duplicate_keys(self) -> None:
        assert len(PERMISSIONS_BY_KEY) == len(PERMISSIONS)

    def test_every_permission_has_a_module_and_description(self) -> None:
        for permission in PERMISSIONS:
            assert permission.module, f"{permission.key} has no module"
            assert permission.description.strip(), f"{permission.key} has no description"

    def test_keys_are_dotted_and_prefixed_by_module(self) -> None:
        for permission in PERMISSIONS:
            assert "." in permission.key
            assert permission.key.startswith(f"{permission.module}.")

    def test_grouping_covers_every_permission(self) -> None:
        grouped = registry_by_module()
        assert sum(len(v) for v in grouped.values()) == len(PERMISSIONS)

    def test_unknown_permission_raises_with_a_useful_message(self) -> None:
        with pytest.raises(UnknownPermissionError) as exc:
            get_permission("proposals.raed")  # typo
        assert "proposals.raed" in str(exc.value)
        assert "app.core.rbac.PERMISSIONS" in str(exc.value)


class TestScopeAlgebra:
    @pytest.mark.parametrize(
        ("granted", "required", "expected"),
        [
            (Scope.ALL, Scope.ALL, True),
            (Scope.ALL, Scope.TEAM, True),
            (Scope.ALL, Scope.OWN, True),
            (Scope.TEAM, Scope.ALL, False),
            (Scope.TEAM, Scope.TEAM, True),
            (Scope.TEAM, Scope.OWN, True),
            (Scope.OWN, Scope.ALL, False),
            (Scope.OWN, Scope.TEAM, False),
            (Scope.OWN, Scope.OWN, True),
        ],
    )
    def test_satisfies(self, granted: Scope, required: Scope, expected: bool) -> None:
        assert granted.satisfies(required) is expected


class TestSystemRoles:
    def test_expected_roles_ship(self) -> None:
        assert set(SYSTEM_ROLES_BY_KEY) == {
            "super_admin",
            "developer",
            "org_manager",
            "team_manager",
            "team_member",
            "viewer",
        }

    def test_super_admin_covers_the_whole_registry(self) -> None:
        granted = {key for key, _ in SYSTEM_ROLES_BY_KEY["super_admin"].grants}
        assert granted == set(PERMISSIONS_BY_KEY)

    def test_developer_has_no_business_write_permission(self) -> None:
        """§4.2: the developer role is deliberately read-only over business data.

        A developer can see everything and operate the platform, but cannot approve a quote
        or reassign a proposal. That separation is the point of having the role at all.
        """
        write_verbs = (".create", ".update", ".delete", ".approve", ".reject", ".assign", ".submit")
        for key, _ in SYSTEM_ROLES_BY_KEY["developer"].grants:
            module = key.split(".", 1)[0]
            if module in {"dev", "admin"}:
                continue
            assert not key.endswith(write_verbs), f"developer must not hold {key}"

    def test_viewer_holds_only_read_permissions(self) -> None:
        for key, _ in SYSTEM_ROLES_BY_KEY["viewer"].grants:
            assert ".read" in key, f"viewer must not hold {key}"

    def test_team_roles_are_marked_team_scoped(self) -> None:
        for key in ("team_manager", "team_member", "viewer"):
            assert SYSTEM_ROLES_BY_KEY[key].is_team_scoped
        for key in ("super_admin", "developer", "org_manager"):
            assert not SYSTEM_ROLES_BY_KEY[key].is_team_scoped

    def test_no_role_grants_a_scope_the_permission_forbids(self) -> None:
        for role in SYSTEM_ROLES:
            for key, scope in role.grants:
                assert scope in PERMISSIONS_BY_KEY[key].scopes


# ──────────────────────────────────────────────────────────────────────────
TEAM_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
TEAM_B = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _principal(**kwargs: object) -> Principal:
    defaults: dict[str, object] = {
        "user_id": uuid.uuid4(),
        "email": "member@hamdaz.com",
        "display_name": "Test Member",
    }
    defaults.update(kwargs)
    return Principal(**defaults)  # type: ignore[arg-type]


class TestPrincipalResolution:
    def test_team_grant_does_not_leak_to_another_team(self) -> None:
        """The single most important assertion in this file.

        A manager of Pre-Sales must not be able to approve Business Development's quotes.
        """
        principal = _principal(
            teams={
                TEAM_A: TeamGrant(TEAM_A, "pre-sales", "team_manager", {"quotes.approve": Scope.TEAM}),
                TEAM_B: TeamGrant(TEAM_B, "bd", "team_member", {}),
            }
        )
        assert principal.has("quotes.approve", Scope.TEAM, team_id=TEAM_A)
        assert not principal.has("quotes.approve", Scope.TEAM, team_id=TEAM_B)

    def test_team_grant_does_not_satisfy_an_org_wide_check(self) -> None:
        """A team-scoped grant is not an org-wide one, even for the same permission."""
        principal = _principal(
            teams={TEAM_A: TeamGrant(TEAM_A, "pre-sales", "team_manager", {"reports.read_team": Scope.TEAM})}
        )
        assert principal.has("reports.read_team", Scope.TEAM, team_id=TEAM_A)
        assert not principal.has("reports.read_team", Scope.ALL, team_id=TEAM_A)

    def test_team_grant_is_ignored_when_no_team_is_named(self) -> None:
        principal = _principal(
            teams={TEAM_A: TeamGrant(TEAM_A, "pre-sales", "team_manager", {"quotes.approve": Scope.TEAM})}
        )
        assert not principal.has("quotes.approve", Scope.TEAM)

    def test_org_grant_applies_in_every_team(self) -> None:
        principal = _principal(
            org_permissions={"quotes.approve": Scope.ALL},
            teams={TEAM_A: TeamGrant(TEAM_A, "pre-sales", "org_manager", {})},
        )
        assert principal.has("quotes.approve", Scope.ALL)
        assert principal.has("quotes.approve", Scope.TEAM, team_id=TEAM_B)

    def test_super_admin_short_circuits_everything(self) -> None:
        principal = _principal(is_super_admin=True)
        assert principal.has("dev.tools.create", Scope.ALL)
        assert principal.has("quotes.approve", Scope.ALL, team_id=TEAM_B)
        assert principal.effective_scope("proposals.delete") is Scope.ALL

    def test_widest_scope_wins_when_grants_overlap(self) -> None:
        principal = _principal(
            org_permissions={"proposals.read": Scope.OWN},
            teams={TEAM_A: TeamGrant(TEAM_A, "pre-sales", "team_member", {"proposals.read": Scope.TEAM})},
        )
        assert principal.effective_scope("proposals.read", team_id=TEAM_A) is Scope.TEAM

    def test_missing_permission_resolves_to_none(self) -> None:
        assert _principal().effective_scope("proposals.read") is None

    def test_typo_in_permission_key_raises_rather_than_denying(self) -> None:
        """A silent False here would be a permission check that can never pass."""
        with pytest.raises(UnknownPermissionError):
            _principal().has("quotes.aprove")


class TestSuperAdminReachesEveryTeam:
    """A super admin administers departments they hold no membership row in.

    ``has()`` already short-circuits, but ``teams_with()`` is what team pickers and
    team-scoped list queries enumerate — so it has to agree, or a super admin silently
    cannot reach half the organisation.
    """

    def test_teams_with_covers_every_known_team(self) -> None:
        principal = _principal(
            is_super_admin=True,
            teams={
                TEAM_A: TeamGrant(TEAM_A, "pre-sales", "super_admin", {}),
                TEAM_B: TeamGrant(TEAM_B, "bd", "super_admin", {}),
            },
        )
        assert principal.teams_with("rules.read") == frozenset({TEAM_A, TEAM_B})
        assert principal.teams_with("proposals.assign") == frozenset({TEAM_A, TEAM_B})

    def test_permission_holds_in_a_team_with_no_grants(self) -> None:
        """The TeamGrant carries empty permissions; is_super_admin answers first."""
        principal = _principal(
            is_super_admin=True,
            teams={TEAM_B: TeamGrant(TEAM_B, "bd", "super_admin", {})},
        )
        assert principal.has("quotes.approve", Scope.TEAM, team_id=TEAM_B)
        assert principal.has("rules.publish", Scope.TEAM, team_id=TEAM_B)

    def test_a_non_super_admin_is_still_confined(self) -> None:
        """The counterpart: widening super admins must not widen anyone else."""
        principal = _principal(
            teams={
                TEAM_A: TeamGrant(TEAM_A, "pre-sales", "team_manager", {"rules.read": Scope.TEAM}),
                TEAM_B: TeamGrant(TEAM_B, "bd", "viewer", {}),
            }
        )
        assert principal.teams_with("rules.read") == frozenset({TEAM_A})


class TestTeamsWith:
    def test_returns_only_qualifying_teams(self) -> None:
        principal = _principal(
            teams={
                TEAM_A: TeamGrant(TEAM_A, "pre-sales", "team_manager", {"proposals.assign": Scope.TEAM}),
                TEAM_B: TeamGrant(TEAM_B, "bd", "viewer", {"proposals.read": Scope.TEAM}),
            }
        )
        assert principal.teams_with("proposals.assign") == frozenset({TEAM_A})

    def test_org_wide_grant_returns_every_team(self) -> None:
        principal = _principal(
            org_permissions={"proposals.assign": Scope.ALL},
            teams={
                TEAM_A: TeamGrant(TEAM_A, "pre-sales", "org_manager", {}),
                TEAM_B: TeamGrant(TEAM_B, "bd", "org_manager", {}),
            },
        )
        assert principal.teams_with("proposals.assign") == frozenset({TEAM_A, TEAM_B})

    def test_empty_when_nothing_qualifies(self) -> None:
        principal = _principal(
            teams={TEAM_A: TeamGrant(TEAM_A, "pre-sales", "viewer", {"proposals.read": Scope.TEAM})}
        )
        assert principal.teams_with("proposals.assign") == frozenset()


class TestLabels:
    def test_org_labels_apply_in_every_team(self) -> None:
        principal = _principal(labels={None: frozenset({"senior"}), TEAM_A: frozenset({"new-joiner"})})
        assert principal.has_label("senior", team_id=TEAM_A)
        assert principal.has_label("senior", team_id=TEAM_B)

    def test_team_labels_do_not_leak(self) -> None:
        principal = _principal(labels={TEAM_A: frozenset({"new-joiner"})})
        assert principal.has_label("new-joiner", team_id=TEAM_A)
        assert not principal.has_label("new-joiner", team_id=TEAM_B)

    def test_labels_are_independent_of_permissions(self) -> None:
        """§4: roles grant permission, labels drive policy — two separate axes."""
        principal = _principal(
            teams={TEAM_A: TeamGrant(TEAM_A, "pre-sales", "team_member", {"proposals.read": Scope.TEAM})},
            labels={TEAM_A: frozenset({"new-joiner"})},
        )
        assert principal.has("proposals.read", Scope.TEAM, team_id=TEAM_A)
        assert principal.has_label("new-joiner", team_id=TEAM_A)
