"""Shared test fixtures.

The environment is pinned *before* anything imports ``app.core.config``, because
Settings is an ``lru_cache`` singleton — once it has read the real ``.env`` there
is no taking it back. Tests therefore run against ``hamdaz_test``, a separate
database, and never touch real rows.
"""

from __future__ import annotations

import os

# ── must precede every app import ──────────────────────────────────────
os.environ["ENVIRONMENT"] = "local"
os.environ["DATABASE_URL"] = (
    "postgresql+psycopg://hamdaz:admin%40hmdz1"
    "@hamdaz.postgres.database.azure.com:5432/hamdaz_test?sslmode=require"
)
os.environ["AZURE_TENANT_ID"] = "test-tenant"
os.environ["AZURE_CLIENT_ID"] = "test-client"
os.environ["AZURE_CLIENT_SECRET"] = "test-secret"
os.environ["AZURE_REDIRECT_URI"] = "http://testserver/api/v1/auth/callback"
os.environ["SESSION_SECRET"] = "test-session-secret-long-enough-for-hmac-sha256"
os.environ["FRONTEND_URL"] = "http://frontend.test"
os.environ["CORS_ORIGINS"] = "http://frontend.test"
os.environ["DEBUG"] = "false"

from collections.abc import AsyncIterator  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import app  # noqa: F401, E402 — sets the Windows event-loop policy
from app.core.config import Settings, get_settings  # noqa: E402
from app.core.db import get_session, get_session_factory  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import Base  # noqa: E402


@pytest.fixture(scope="session")
def settings() -> Settings:
    return get_settings()


@pytest.fixture(scope="session")
async def engine(settings: Settings):
    eng = create_async_engine(settings.database_url, poolclass=None)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await eng.dispose()


@pytest.fixture
def session_factory(engine) -> async_sessionmaker[AsyncSession]:
    """For code that fans out on independent sessions, as dashboards do."""
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def db(engine) -> AsyncIterator[AsyncSession]:
    """A session for arranging fixtures directly, cleaned out after each test."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    # Truncate rather than roll back: the app under test commits through its own
    # session, so a rollback here would not undo what the request did.
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "truncate table users, roles, teams, modules, "
                "leave_requests, leave_settings, labels, "
                "assignment_policies, quote_requests, form_templates, "
                # Named explicitly rather than left to CASCADE: the HR tables
                # hang off form_templates by a RESTRICT foreign key, and a test
                # that leaked an opening would make the next one's seed fail on
                # a template it could not replace.
                "job_openings, job_applications, employee_documents, "
                "review_cycles cascade"
            )
        )


class StubOIDC:
    """Stands in for Entra so route tests never leave the process.

    ``verify_id_token`` is exercised for real in test_oidc.py against a locally
    generated key; here we only care about what the router does with the result.
    """

    def __init__(self) -> None:
        self.identity = None
        self.error: Exception | None = None
        self.authorize_calls: list[dict] = []

    def authorization_url(self, *, state: str, nonce: str, code_challenge: str) -> str:
        self.authorize_calls.append(
            {"state": state, "nonce": nonce, "code_challenge": code_challenge}
        )
        return f"https://login.microsoftonline.com/authorize?state={state}"

    async def exchange_code(self, *, code: str, code_verifier: str) -> str:
        if self.error:
            raise self.error
        return "stub.id.token"

    async def verify_id_token(self, id_token: str, *, nonce: str):
        if self.error:
            raise self.error
        return self.identity


class StubGraph:
    """Stands in for Microsoft Graph in route tests.

    The real paging, filtering and token caching are exercised against a mock
    transport in test_directory.py; here we only care what the router does with
    a result or an error.
    """

    def __init__(self) -> None:
        self.users: list = []
        self.error: Exception | None = None
        self.calls: list[dict] = []

    async def list_users(self, *, include_guests: bool = False, include_disabled: bool = False):
        self.calls.append({"include_guests": include_guests, "include_disabled": include_disabled})
        if self.error:
            raise self.error
        return list(self.users)

    async def get_user(self, object_id: str):
        if self.error:
            raise self.error
        found = next((u for u in self.users if u.object_id == object_id), None)
        if found is None:
            from app.directory.graph import GraphError

            raise GraphError("not found")
        return found


@pytest.fixture
def oidc() -> StubOIDC:
    return StubOIDC()


@pytest.fixture
def graph() -> StubGraph:
    return StubGraph()


@pytest.fixture
async def client(engine, oidc: StubOIDC, graph: StubGraph) -> AsyncIterator[httpx.AsyncClient]:
    """An HTTP client wired to the real app, minus the network."""
    fastapi_app = create_app()
    fastapi_app.state.oidc = oidc
    fastapi_app.state.graph = graph

    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    fastapi_app.dependency_overrides[get_session] = override_get_session
    # The profile aggregator fans out on independent sessions, so it needs the
    # factory rather than the request's session.
    fastapi_app.dependency_overrides[get_session_factory] = lambda: factory

    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
