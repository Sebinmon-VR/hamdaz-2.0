"""The assembled API: routes exist, permissions gate them, and the registries are served.

These run without a database — the DB dependency is overridden — so they cover routing,
authorization and serialisation, which is where most regressions actually land.
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.principal import Principal
from app.core.rules.registry import DECISION_POINTS

PRESALES = uuid.UUID("11111111-1111-1111-1111-111111111111")
BD = uuid.UUID("22222222-2222-2222-2222-222222222222")


class TestRoutesAreMounted:
    def test_openapi_includes_every_module(self, client: TestClient) -> None:
        paths = client.get("/openapi.json").json()["paths"]
        for expected in (
            "/api/v1/auth/login",
            "/api/v1/meta/permissions",
            "/api/v1/admin/teams",
            "/api/v1/rules/decision-points",
            "/api/v1/proposals/",
            "/api/v1/developer/overview",
        ):
            assert expected in paths, f"{expected} is not mounted"

    def test_operation_ids_are_unique(self, client: TestClient) -> None:
        """Duplicates silently break generated clients."""
        paths = client.get("/openapi.json").json()["paths"]
        ids = [
            op["operationId"]
            for methods in paths.values()
            for op in methods.values()
            if isinstance(op, dict) and "operationId" in op
        ]
        assert len(ids) == len(set(ids))


class TestDecisionPointRegistry:
    def test_served_registry_matches_the_code(self, client: TestClient) -> None:
        served = client.get("/api/v1/rules/decision-points").json()
        assert {d["key"] for d in served} == {d.key for d in DECISION_POINTS}

    def test_each_decision_point_ships_facts_and_actions(self, client: TestClient) -> None:
        """The rule builder renders from this, so an empty list would mean an unusable screen."""
        for dp in client.get("/api/v1/rules/decision-points").json():
            assert dp["facts"], f"{dp['key']} serves no facts"
            assert dp["actions"], f"{dp['key']} serves no actions"

    def test_facts_carry_their_valid_operators(self, client: TestClient) -> None:
        for dp in client.get("/api/v1/rules/decision-points").json():
            for fact in dp["facts"]:
                assert fact["operators"]

    def test_assignment_decision_point_is_present(self, client: TestClient) -> None:
        keys = {d["key"] for d in client.get("/api/v1/rules/decision-points").json()}
        assert "proposal.assign" in keys
        assert "quote.approval_route" in keys


class TestAdminPermissions:
    def test_team_member_cannot_create_a_team(self, as_user, member_principal: Principal) -> None:  # type: ignore[no-untyped-def]
        response = as_user(member_principal).post(
            "/api/v1/admin/teams", json={"name": "Rogue Team"}
        )
        assert response.status_code == 403

    def test_manager_cannot_create_a_team(self, as_user, manager_principal: Principal) -> None:  # type: ignore[no-untyped-def]
        """Team management is org-scoped; managing a team is not the same as creating one."""
        response = as_user(manager_principal).post(
            "/api/v1/admin/teams", json={"name": "Rogue Team"}
        )
        assert response.status_code == 403

    def test_member_cannot_create_a_custom_role(self, as_user, member_principal: Principal) -> None:  # type: ignore[no-untyped-def]
        response = as_user(member_principal).post(
            "/api/v1/admin/roles",
            json={"key": "sneaky", "name": "Sneaky", "grants": {"quotes.approve": "all"}},
        )
        assert response.status_code == 403

    def test_available_modules_are_listed(self, as_user, member_principal: Principal) -> None:  # type: ignore[no-untyped-def]
        modules = as_user(member_principal).get("/api/v1/admin/teams/modules").json()
        assert "proposals" in modules
        assert "leave" in modules


class TestDeveloperPanelIsGated:
    def test_member_cannot_reach_the_developer_panel(self, as_user, member_principal: Principal) -> None:  # type: ignore[no-untyped-def]
        client = as_user(member_principal)
        for path in ("/overview", "/jobs", "/connectors", "/rule-evaluations", "/audit"):
            assert client.get(f"/api/v1/developer{path}").status_code == 403, path

    def test_team_manager_cannot_reach_it_either(self, as_user, manager_principal: Principal) -> None:  # type: ignore[no-untyped-def]
        """dev.* is org-scoped: a team grant must never satisfy it."""
        assert as_user(manager_principal).get("/api/v1/developer/overview").status_code == 403

    def test_unauthenticated_is_401_not_403(self, client: TestClient) -> None:
        assert client.get("/api/v1/developer/overview").status_code == 401


class TestRulesPermissions:
    def test_member_cannot_create_a_rule_set(self, as_user, member_principal: Principal) -> None:  # type: ignore[no-untyped-def]
        response = as_user(member_principal).post(
            "/api/v1/rules/sets",
            json={
                "decision_point": "quote.approval_route",
                "name": "Mine",
                "team_id": str(PRESALES),
            },
        )
        assert response.status_code == 403

    def test_manager_cannot_create_a_rule_set_for_another_team(
        self, as_user, manager_principal: Principal
    ) -> None:  # type: ignore[no-untyped-def]
        response = as_user(manager_principal).post(
            "/api/v1/rules/sets",
            json={
                "decision_point": "quote.approval_route",
                "name": "Theirs",
                "team_id": str(BD),
            },
        )
        assert response.status_code == 403

    def test_decision_points_are_public_to_signed_in_users(self, client: TestClient) -> None:
        """The registry is not sensitive; it is the vocabulary, not the policy."""
        assert client.get("/api/v1/rules/decision-points").status_code == 200


class TestProposalsPermissions:
    def test_member_cannot_create_in_another_team(self, as_user, member_principal: Principal) -> None:  # type: ignore[no-untyped-def]
        response = as_user(member_principal).post(
            "/api/v1/proposals/", json={"team_id": str(BD), "title": "Sneaky proposal"}
        )
        assert response.status_code == 403

    def test_member_cannot_list_another_teams_proposals(
        self, as_user, member_principal: Principal
    ) -> None:  # type: ignore[no-untyped-def]
        response = as_user(member_principal).get(f"/api/v1/proposals/?team_id={BD}")
        assert response.status_code == 403

    def test_member_cannot_see_a_teams_workload(self, as_user, member_principal: Principal) -> None:  # type: ignore[no-untyped-def]
        """Workload needs reports.read_team, which a plain member does not hold."""
        assert (
            as_user(member_principal).get(f"/api/v1/proposals/workload/{PRESALES}").status_code
            == 403
        )


class TestAuthEndpoints:
    def test_login_reports_when_azure_is_unconfigured(self, client: TestClient) -> None:
        """A blank tenant should say so, not produce a broken Microsoft redirect."""
        response = client.get("/api/v1/auth/login?redirect=false")
        assert response.status_code == 401
        assert "not configured" in response.json()["detail"]

    def test_callback_requires_the_flow_cookies(self, client: TestClient) -> None:
        response = client.get("/api/v1/auth/callback?code=abc&state=xyz")
        assert response.status_code == 401

    def test_logout_requires_a_session(self, client: TestClient) -> None:
        assert client.post("/api/v1/auth/logout").status_code == 401


class TestErrorContract:
    def test_permission_denied_names_the_permission(
        self, app: FastAPI, as_user, member_principal: Principal
    ) -> None:  # type: ignore[no-untyped-def]
        """The frontend uses this to explain the refusal rather than saying 'forbidden'."""
        body = as_user(member_principal).post(
            "/api/v1/admin/teams", json={"name": "Nope"}
        ).json()
        assert body["permission"] == "admin.teams.manage"
        assert body["scope"] == "all"
        assert body["correlation_id"]

    def test_validation_errors_are_problem_documents(
        self, as_user, super_admin_principal: Principal
    ) -> None:  # type: ignore[no-untyped-def]
        response = as_user(super_admin_principal).post("/api/v1/admin/teams", json={"name": "x"})
        assert response.status_code == 422
        assert response.headers["content-type"].startswith("application/problem+json")
        assert "errors" in response.json()
