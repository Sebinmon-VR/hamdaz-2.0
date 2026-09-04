"""FastAPI application."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.access.router import router as access_router
from app.analytics.router import router as analytics_router
from app.assignment.router import router as assignment_router
from app.auth.oidc import EntraOIDC
from app.auth.router import router as auth_router
from app.comparison.extraction import QuoteExtractor
from app.comparison.router import router as comparison_router
from app.core.config import get_settings
from app.core.db import dispose_engine, get_session_factory, init_engine
from app.dashboards.router import router as dashboards_router
from app.directory.graph import GraphDirectory
from app.directory.router import router as directory_router
from app.forms.router import router as templates_router
from app.labels.router import router as labels_router
from app.leave.mailer import LeaveMailer
from app.leave.router import router as leave_router
from app.profiles.router import router as profiles_router
from app.proposals.analytics import WorkloadCache
from app.proposals.router import router as proposals_router
from app.proposals.sharepoint import SharePointProposals
from app.quoting.mailer import QuoteMailer
from app.quoting.probability import WinRates
from app.quoting.router import router as quoting_router
from app.roles.router import router as roles_router
from app.teams.router import router as teams_router
from app.zoho.cache import QuoteCache
from app.zoho.client import ZohoBooks
from app.zoho.router import router as zoho_router

logging.basicConfig(level=logging.INFO, format="%(levelname)-5s [%(name)s] %(message)s")
logger = logging.getLogger("hamdaz")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    settings.validate_runtime()  # fail at boot, not at someone's first login

    init_engine(settings)
    # One connection pool for the whole process — Entra's token and JWKS
    # endpoints are called on every sign-in, so per-request clients would
    # re-handshake TLS each time.
    http = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0))
    app.state.http = http
    app.state.oidc = EntraOIDC(settings, http)
    # Shares the HTTP client and caches its own app-only token.
    app.state.graph = GraphDirectory(settings, http)
    # Read-only client for the Proposals list; caches its own token.
    app.state.sharepoint = SharePointProposals(settings, http)
    # Shared by every admin: the aggregate is identical for all of them.
    app.state.workload_cache = WorkloadCache()
    # Sends as the requester. Disabled unless HR turns it on in leave settings.
    app.state.leave_mailer = LeaveMailer(settings, http)
    # Reads supplier quote documents. Holds no connection of its own; the
    # Anthropic SDK manages that, and an unset key fails at the endpoint rather
    # than at boot so the manual entry path keeps working without one.
    app.state.quote_extractor = QuoteExtractor(settings)
    # Read-only over Zoho Books. Its access token is shared through Postgres
    # rather than held per process — see app/zoho/tokens.py for why.
    app.state.zoho = ZohoBooks(settings, http, get_session_factory())
    # The quotes list is the same for everyone, so one cache serves all callers.
    app.state.quote_cache = QuoteCache()
    # Win rates over the estimate history. One sweep serves every draft, and
    # it is a read of Zoho only — nothing is written there.
    app.state.win_rates = WinRates()
    # Approvers are told a quote is waiting, from the requester's own mailbox.
    app.state.quote_mailer = QuoteMailer(settings, http)
    logger.info("started environment=%s", settings.environment)

    try:
        yield
    finally:
        await http.aclose()
        await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        # Required for the session cookie to travel on frontend XHR. Note this
        # forbids the "*" origin wildcard — hence an explicit list.
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(auth_router, prefix=settings.api_prefix)
    app.include_router(directory_router, prefix=settings.api_prefix)
    app.include_router(roles_router, prefix=settings.api_prefix)
    app.include_router(teams_router, prefix=settings.api_prefix)
    app.include_router(profiles_router, prefix=settings.api_prefix)
    app.include_router(access_router, prefix=settings.api_prefix)
    app.include_router(dashboards_router, prefix=settings.api_prefix)
    app.include_router(proposals_router, prefix=settings.api_prefix)
    app.include_router(leave_router, prefix=settings.api_prefix)
    app.include_router(zoho_router, prefix=settings.api_prefix)
    app.include_router(comparison_router, prefix=settings.api_prefix)
    app.include_router(labels_router, prefix=settings.api_prefix)
    app.include_router(assignment_router, prefix=settings.api_prefix)
    app.include_router(analytics_router, prefix=settings.api_prefix)
    app.include_router(quoting_router, prefix=settings.api_prefix)
    app.include_router(templates_router, prefix=settings.api_prefix)

    # Dev console. Mounted, not merely guarded — in production the route does
    # not exist at all, so there is nothing to accidentally expose.
    if settings.environment == "local":
        from app.sandbox.router import router as sandbox_router

        app.include_router(sandbox_router)
        logger.info("dev sandbox mounted at /sandbox")

    @app.get("/health", tags=["ops"])
    async def health() -> dict[str, str]:
        # Deliberately does not touch Postgres: this answers "is the process up",
        # which is what a platform health probe should restart on.
        return {"status": "ok", "app": settings.app_name}

    return app


app = create_app()
