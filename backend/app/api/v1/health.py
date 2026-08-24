"""Liveness and readiness probes."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Response, status
from pydantic import BaseModel
from sqlalchemy import text

from app.api.deps import DbDep, SettingsDep
from app.core.logging import get_logger

logger = get_logger(__name__)
router = APIRouter(tags=["health"])


class HealthOut(BaseModel):
    status: Literal["ok"]
    app: str
    environment: str


class ReadyOut(BaseModel):
    status: Literal["ready", "degraded"]
    checks: dict[str, str]


@router.get("/health", response_model=HealthOut)
async def health(settings: SettingsDep) -> HealthOut:
    """Liveness: the process is up. Deliberately touches no dependency."""
    return HealthOut(status="ok", app=settings.app_name, environment=settings.environment.value)


@router.get("/ready", response_model=ReadyOut)
async def ready(session: DbDep, response: Response) -> ReadyOut:
    """Readiness: dependencies are reachable, so it is safe to route traffic here."""
    checks: dict[str, str] = {}

    try:
        await session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        logger.warning("readiness.database_failed", error=str(exc))
        checks["database"] = f"error: {type(exc).__name__}"

    degraded = any(v != "ok" for v in checks.values())
    if degraded:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ReadyOut(status="degraded" if degraded else "ready", checks=checks)
