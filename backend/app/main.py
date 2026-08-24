"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1 import admin, auth, developer, health, meta, proposals, rules
from app.core.compat import configure_event_loop_policy
from app.core.config import Settings, get_settings
from app.core.db import dispose_engine, init_engine
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.middleware import AccessLogMiddleware, CorrelationIdMiddleware
from app.core.rbac import PERMISSIONS, validate_registry
from app.core.rules.registry import DECISION_POINTS

logger = get_logger(__name__)

# Must run before uvicorn builds its loop: psycopg3 cannot use Windows' default
# ProactorEventLoop. No-op on other platforms.
configure_event_loop_policy()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings

    # Fail at startup rather than at the first 403: catches a role granting a permission
    # that does not exist, or one at a scope the permission does not support.
    validate_registry()

    init_engine(settings)
    logger.info(
        "app.started",
        environment=settings.environment.value,
        permissions=len(PERMISSIONS),
        decision_points=len(DECISION_POINTS),
        sharepoint_sandbox_writes=settings.sharepoint_sandbox_writes_enabled,
        outbound_email=settings.outbound_email_enabled,
    )
    try:
        yield
    finally:
        await dispose_engine()
        logger.info("app.stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description="Hamdaz 2.0 ERP — multi-team platform API.",
        lifespan=lifespan,
        docs_url="/docs" if not settings.is_production else None,
        redoc_url=None,
        openapi_url="/openapi.json" if not settings.is_production else None,
    )
    app.state.settings = settings

    # Middleware runs bottom-up, so the correlation ID is bound before anything logs.
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(CorrelationIdMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Correlation-ID", "X-Response-Time-ms"],
    )

    register_exception_handlers(app)

    # Probes sit at the root so infrastructure does not need to know the API version.
    app.include_router(health.router)

    v1 = APIRouter(prefix=settings.api_v1_prefix)
    v1.include_router(auth.router)
    v1.include_router(meta.router)
    v1.include_router(admin.router)
    v1.include_router(rules.router)
    v1.include_router(proposals.router)
    v1.include_router(developer.router)
    app.include_router(v1)

    return app


app = create_app()
