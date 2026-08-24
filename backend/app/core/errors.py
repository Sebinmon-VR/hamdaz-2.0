"""RFC 7807 problem responses.

Every error the API returns has the same shape, and every one carries the correlation ID —
so a user reporting a failure can quote a single value that pulls up the full trace in the
developer panel.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_correlation_id, get_logger

logger = get_logger(__name__)

PROBLEM_CONTENT_TYPE = "application/problem+json"


class AppError(Exception):
    """Base class for expected, user-facing failures.

    Anything that subclasses this is a condition we anticipated and can explain. Unexpected
    exceptions fall through to the catch-all handler and become an opaque 500 — deliberately,
    so an internal message never leaks to a browser.
    """

    status_code: int = status.HTTP_400_BAD_REQUEST
    title: str = "Request failed"
    error_code: str = "app_error"

    def __init__(self, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.detail = detail
        self.extra = extra


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    title = "Not found"
    error_code = "not_found"


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    title = "Conflict"
    error_code = "conflict"


class AuthenticationError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    title = "Not authenticated"
    error_code = "not_authenticated"


class PermissionDeniedError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    title = "Permission denied"
    error_code = "permission_denied"

    def __init__(self, detail: str, *, permission: str | None = None, scope: str | None = None):
        super().__init__(detail, permission=permission, scope=scope)


class ValidationError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    title = "Validation failed"
    error_code = "validation_failed"


def _problem(
    *,
    status_code: int,
    title: str,
    detail: str,
    error_code: str,
    instance: str,
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": f"https://docs.hamdaz.internal/errors/{error_code}",
        "title": title,
        "status": status_code,
        "detail": detail,
        "instance": instance,
        "correlation_id": get_correlation_id(),
    }
    if extra:
        body.update({k: v for k, v in extra.items() if v is not None})
    return JSONResponse(status_code=status_code, content=body, media_type=PROBLEM_CONTENT_TYPE)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        logger.info(
            "request.failed",
            error_code=exc.error_code,
            status=exc.status_code,
            detail=exc.detail,
            path=request.url.path,
        )
        return _problem(
            status_code=exc.status_code,
            title=exc.title,
            detail=exc.detail,
            error_code=exc.error_code,
            instance=request.url.path,
            extra=exc.extra,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _problem(
            status_code=exc.status_code,
            title=str(exc.detail),
            detail=str(exc.detail),
            error_code="http_error",
            instance=request.url.path,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _problem(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            title="Validation failed",
            detail="The request body or parameters were not valid.",
            error_code="validation_failed",
            instance=request.url.path,
            extra={"errors": exc.errors()},
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Log the full detail; return none of it.
        logger.exception("request.unhandled", path=request.url.path, error=str(exc))
        return _problem(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            title="Internal server error",
            detail="Something went wrong. Quote the correlation ID when reporting this.",
            error_code="internal_error",
            instance=request.url.path,
        )
