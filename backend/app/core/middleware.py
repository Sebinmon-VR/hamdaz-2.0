"""Request middleware: correlation IDs and access logging.

Every request gets an ID, every log line carries it, and it comes back in the response
header and in every error body. That single thread is what makes the developer panel's
request tracing (§6.1) work.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import bind_correlation_id, get_logger

logger = get_logger(__name__)

CORRELATION_HEADER = "X-Correlation-ID"

#: Health probes fire constantly and would drown the activity feed.
_QUIET_PATHS = frozenset({"/health", "/ready", "/metrics"})


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # Honour an inbound ID so a trace can span the frontend and the API.
        incoming = request.headers.get(CORRELATION_HEADER)
        correlation_id = bind_correlation_id(incoming)
        request.state.correlation_id = correlation_id

        response = await call_next(request)
        response.headers[CORRELATION_HEADER] = correlation_id
        return response


class AccessLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path in _QUIET_PATHS:
            return await call_next(request)

        started = time.perf_counter()
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - started) * 1000, 2)

        principal = getattr(request.state, "principal", None)

        logger.info(
            "request.completed",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=duration_ms,
            actor=str(principal.user_id) if principal else None,
        )
        response.headers["X-Response-Time-ms"] = str(duration_ms)
        return response
