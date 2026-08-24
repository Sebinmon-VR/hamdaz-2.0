"""Shared fixtures.

These build a real app with the database dependency replaced, so the API surface and the
authorization wiring are exercised without needing Postgres. Tests that genuinely need a
database are marked ``integration`` and skipped unless one is configured.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_current_principal
from app.core.config import Environment, Settings, get_settings
from app.core.db import get_db
from app.core.principal import Principal, TeamGrant
from app.core.rbac import Scope
from app.main import create_app

TEAM_PRESALES = uuid.UUID("11111111-1111-1111-1111-111111111111")
TEAM_BD = uuid.UUID("22222222-2222-2222-2222-222222222222")


@pytest.fixture
def settings() -> Settings:
    """Hermetic settings.

    ``_env_file=None`` stops pydantic-settings reading the developer's ``.env``. Without it
    the suite passes or fails depending on whether the machine happens to have Azure
    credentials configured, which makes it useless as a signal.
    """
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment=Environment.LOCAL,
        jwt_secret="test-secret-not-a-real-key",
        log_format="console",
        log_level="WARNING",
        azure_tenant_id="",
        azure_client_id="",
        azure_client_secret="",
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    application = create_app(settings)

    # Routes resolve settings through the cached get_settings(), which reads the developer's
    # .env. Override it so the whole app sees the hermetic fixture.
    application.dependency_overrides[get_settings] = lambda: settings

    async def _no_db() -> None:
        # Nothing in the non-integration suite should reach the database. If something
        # does, this makes it a loud AttributeError rather than a hang on connect.
        return None

    application.dependency_overrides[get_db] = _no_db
    return application


@pytest.fixture
def member_principal() -> Principal:
    """A Pre-Sales team member. Deliberately holds no Business Development grants."""
    return Principal(
        user_id=uuid.uuid4(),
        email="member@hamdaz.com",
        display_name="Aisha Member",
        teams={
            TEAM_PRESALES: TeamGrant(
                team_id=TEAM_PRESALES,
                team_slug="pre-sales",
                role_key="team_member",
                permissions={
                    "proposals.read": Scope.TEAM,
                    "quotes.create": Scope.TEAM,
                },
            )
        },
        labels={TEAM_PRESALES: frozenset({"new-joiner"})},
    )


@pytest.fixture
def manager_principal() -> Principal:
    return Principal(
        user_id=uuid.uuid4(),
        email="manager@hamdaz.com",
        display_name="Rahul Manager",
        teams={
            TEAM_PRESALES: TeamGrant(
                team_id=TEAM_PRESALES,
                team_slug="pre-sales",
                role_key="team_manager",
                permissions={
                    "proposals.read": Scope.TEAM,
                    "proposals.assign": Scope.TEAM,
                    "quotes.approve": Scope.TEAM,
                },
            )
        },
    )


@pytest.fixture
def super_admin_principal() -> Principal:
    return Principal(
        user_id=uuid.uuid4(),
        email="admin@hamdaz.com",
        display_name="Site Admin",
        is_super_admin=True,
    )


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """Unauthenticated client."""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def as_user(app: FastAPI):  # type: ignore[no-untyped-def]
    """Factory: return a TestClient authenticated as the given principal."""

    def _factory(principal: Principal) -> TestClient:
        async def _principal() -> Principal:
            return principal

        app.dependency_overrides[get_current_principal] = _principal
        return TestClient(app)

    yield _factory
    app.dependency_overrides.pop(get_current_principal, None)
