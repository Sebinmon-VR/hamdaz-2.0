"""API surface: the app boots, errors have one shape, and the registry is served."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient

from app.api.deps import require, require_any
from app.core.principal import Principal
from app.core.rbac import PERMISSIONS, Scope
from app.core.security import create_access_token

PRESALES = uuid.UUID("11111111-1111-1111-1111-111111111111")
BD = uuid.UUID("22222222-2222-2222-2222-222222222222")


class TestHealth:
    def test_liveness_touches_no_dependency(self, client: TestClient) -> None:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_correlation_id_is_returned(self, client: TestClient) -> None:
        response = client.get("/health")
        assert response.headers["X-Correlation-ID"]

    def test_inbound_correlation_id_is_honoured(self, client: TestClient) -> None:
        """A trace should span the frontend and the API, not restart at the boundary."""
        response = client.get("/health", headers={"X-Correlation-ID": "trace-abc-123"})
        assert response.headers["X-Correlation-ID"] == "trace-abc-123"


class TestErrorShape:
    def test_unknown_route_is_a_problem_document(self, client: TestClient) -> None:
        response = client.get("/api/v1/does-not-exist")
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/problem+json")
        body = response.json()
        assert {"type", "title", "status", "detail", "instance"} <= body.keys()

    def test_errors_carry_the_correlation_id(self, client: TestClient) -> None:
        """This is the value a user quotes in a bug report."""
        response = client.get("/api/v1/nope", headers={"X-Correlation-ID": "trace-xyz"})
        assert response.json()["correlation_id"] == "trace-xyz"

    def test_missing_token_is_401_not_500(self, client: TestClient) -> None:
        response = client.get("/api/v1/meta/me")
        assert response.status_code == 401
        assert response.json()["type"].endswith("not_authenticated")


class TestMetaRegistry:
    def test_permissions_endpoint_serves_the_whole_registry(self, client: TestClient) -> None:
        """§4.3: the admin UI renders from exactly what the backend enforces."""
        response = client.get("/api/v1/meta/permissions")
        assert response.status_code == 200

        served = {p["key"] for module in response.json() for p in module["permissions"]}
        assert served == {p.key for p in PERMISSIONS}

    def test_every_served_permission_declares_its_scopes(self, client: TestClient) -> None:
        for module in client.get("/api/v1/meta/permissions").json():
            for permission in module["permissions"]:
                assert permission["scopes"]
                assert set(permission["scopes"]) <= {"own", "team", "all"}

    def test_system_roles_are_served(self, client: TestClient) -> None:
        keys = {role["key"] for role in client.get("/api/v1/meta/roles").json()}
        assert "team_manager" in keys
        assert "super_admin" in keys

    def test_me_reports_teams_and_labels(self, as_user, member_principal: Principal) -> None:  # type: ignore[no-untyped-def]
        body = as_user(member_principal).get("/api/v1/meta/me").json()
        assert body["email"] == "member@hamdaz.com"
        assert body["is_super_admin"] is False

        (team,) = body["teams"]
        assert team["slug"] == "pre-sales"
        assert team["labels"] == ["new-joiner"]
        assert team["permissions"]["proposals.read"] == "team"


# ──────────────────────────────────────────────────────────────────────────
# The require() dependency, exercised against a throwaway router.
# ──────────────────────────────────────────────────────────────────────────


def _guarded_app(app: FastAPI) -> FastAPI:
    router = APIRouter(prefix="/api/v1/_test")

    @router.get("/team/{team_id}/proposals", dependencies=[Depends(require("proposals.read"))])
    async def read_proposals(team_id: uuid.UUID) -> dict[str, str]:
        return {"team_id": str(team_id)}

    @router.post("/team/{team_id}/assign", dependencies=[Depends(require("proposals.assign"))])
    async def assign(team_id: uuid.UUID) -> dict[str, str]:
        return {"assigned": "ok"}

    @router.get("/org/report", dependencies=[Depends(require("reports.read_org", Scope.ALL))])
    async def org_report() -> dict[str, str]:
        return {"report": "ok"}

    @router.get(
        "/team/{team_id}/either",
        dependencies=[Depends(require_any("proposals.assign", "quotes.approve"))],
    )
    async def either(team_id: uuid.UUID) -> dict[str, str]:
        return {"ok": "yes"}

    app.include_router(router)
    return app


class TestPermissionEnforcement:
    def test_member_can_read_own_team(self, app: FastAPI, as_user, member_principal: Principal) -> None:  # type: ignore[no-untyped-def]
        _guarded_app(app)
        response = as_user(member_principal).get(f"/api/v1/_test/team/{PRESALES}/proposals")
        assert response.status_code == 200

    def test_member_cannot_read_another_team(self, app: FastAPI, as_user, member_principal: Principal) -> None:  # type: ignore[no-untyped-def]
        """The assertion that matters: team isolation holds at the HTTP boundary."""
        _guarded_app(app)
        response = as_user(member_principal).get(f"/api/v1/_test/team/{BD}/proposals")
        assert response.status_code == 403
        assert response.json()["permission"] == "proposals.read"

    def test_member_cannot_assign(self, app: FastAPI, as_user, member_principal: Principal) -> None:  # type: ignore[no-untyped-def]
        _guarded_app(app)
        response = as_user(member_principal).post(f"/api/v1/_test/team/{PRESALES}/assign")
        assert response.status_code == 403

    def test_manager_can_assign_in_own_team(self, app: FastAPI, as_user, manager_principal: Principal) -> None:  # type: ignore[no-untyped-def]
        _guarded_app(app)
        response = as_user(manager_principal).post(f"/api/v1/_test/team/{PRESALES}/assign")
        assert response.status_code == 200

    def test_manager_cannot_assign_in_another_team(self, app: FastAPI, as_user, manager_principal: Principal) -> None:  # type: ignore[no-untyped-def]
        _guarded_app(app)
        response = as_user(manager_principal).post(f"/api/v1/_test/team/{BD}/assign")
        assert response.status_code == 403

    def test_team_manager_cannot_reach_an_org_scoped_route(self, app: FastAPI, as_user, manager_principal: Principal) -> None:  # type: ignore[no-untyped-def]
        """A team grant must never satisfy a Scope.ALL requirement."""
        _guarded_app(app)
        assert as_user(manager_principal).get("/api/v1/_test/org/report").status_code == 403

    def test_super_admin_reaches_everything(self, app: FastAPI, as_user, super_admin_principal: Principal) -> None:  # type: ignore[no-untyped-def]
        _guarded_app(app)
        client = as_user(super_admin_principal)
        assert client.get("/api/v1/_test/org/report").status_code == 200
        assert client.post(f"/api/v1/_test/team/{BD}/assign").status_code == 200

    def test_require_any_passes_on_either_permission(self, app: FastAPI, as_user, manager_principal: Principal) -> None:  # type: ignore[no-untyped-def]
        _guarded_app(app)
        response = as_user(manager_principal).get(f"/api/v1/_test/team/{PRESALES}/either")
        assert response.status_code == 200

    def test_require_any_denies_when_none_match(self, app: FastAPI, as_user, member_principal: Principal) -> None:  # type: ignore[no-untyped-def]
        _guarded_app(app)
        response = as_user(member_principal).get(f"/api/v1/_test/team/{PRESALES}/either")
        assert response.status_code == 403


class TestSessionTokens:
    def test_round_trip(self, settings) -> None:  # type: ignore[no-untyped-def]
        user_id = uuid.uuid4()
        token, expires_at = create_access_token(user_id=user_id, settings=settings)

        from app.core.security import user_id_from_token

        assert user_id_from_token(token, settings) == user_id
        assert expires_at.timestamp() > 0

    def test_token_carries_no_permissions(self, settings) -> None:  # type: ignore[no-untyped-def]
        """Permissions are resolved per request, so a role change takes effect immediately
        rather than whenever the token happens to expire."""
        from app.core.security import decode_access_token

        claims = decode_access_token(
            create_access_token(user_id=uuid.uuid4(), settings=settings)[0], settings
        )
        assert set(claims) == {"sub", "typ", "sid", "iat", "exp"}

    def test_tampered_token_is_rejected(self, settings) -> None:  # type: ignore[no-untyped-def]
        from app.core.errors import AuthenticationError
        from app.core.security import user_id_from_token

        token, _ = create_access_token(user_id=uuid.uuid4(), settings=settings)
        import pytest

        with pytest.raises(AuthenticationError):
            user_id_from_token(token[:-4] + "aaaa", settings)

    def test_expired_token_is_rejected(self, settings) -> None:  # type: ignore[no-untyped-def]
        import pytest

        from app.core.errors import AuthenticationError
        from app.core.security import user_id_from_token

        token, _ = create_access_token(user_id=uuid.uuid4(), settings=settings, expires_in=-60)
        with pytest.raises(AuthenticationError):
            user_id_from_token(token, settings)
