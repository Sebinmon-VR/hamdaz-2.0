"""A local-only dev console for exercising modules by hand.

Mounted only when ``ENVIRONMENT=local`` — see ``create_app``. It is a developer
tool with no authentication of its own, so it must never exist in a deployed
environment. The guard lives at mount time rather than inside the handler so
the route is genuinely absent, not merely refusing.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["sandbox"], include_in_schema=False)

_CONSOLE = Path(__file__).parent / "console.html"


@router.get("/sandbox", response_class=HTMLResponse)
async def console() -> HTMLResponse:
    # Read per request so editing the page does not need a server restart.
    return HTMLResponse(_CONSOLE.read_text(encoding="utf-8"))


@router.get("/login", response_class=HTMLResponse)
async def login_landing() -> HTMLResponse:
    """Where a failed sign-in is sent.

    The router redirects failures to ``{FRONTEND_URL}/login?error=...``. In a
    deployed setup that is the real frontend's login page; locally nothing is
    listening there, so the console stands in and surfaces the error instead of
    a bare 404.
    """
    return HTMLResponse(_CONSOLE.read_text(encoding="utf-8"))
