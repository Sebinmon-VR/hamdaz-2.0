"""What every unauthenticated endpoint shares: its headers and its brake.

Two routers face the public — the careers forms (``app.hr.public``) and the
website's contact form (``app.website.public``). Both are mounted outside the
API prefix and both must look the same from outside: no credentials, no
referrer, nothing cached, and a crude limit on one address hammering a form.
One copy of that here, rather than two that drift apart until one of them
quietly allows credentials.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Final

from fastapi import Request, Response
from starlette.middleware.cors import CORSMiddleware
from starlette.types import Receive, Scope, Send

#: Headers on everything a public router serves. The CSP is the important one
#: for the careers router's hosted page: it is entirely self-contained, so
#: anything it could be made to load from elsewhere is an injection rather than
#: a feature. On a JSON answer it costs nothing.
SECURITY_HEADERS: Final[dict[str, str]] = {
    "Access-Control-Allow-Origin": "*",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
        "script-src 'unsafe-inline'; form-action 'self'; connect-src 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    ),
}


def harden(response: Response) -> None:
    """Apply the headers above without stamping on the CORS middleware.

    Where the request came from an allowlisted ERP origin the middleware sets
    its own, more specific ``Access-Control-Allow-Origin`` after this runs, and
    a plain assignment there replaces rather than appends.
    """
    for name, value in SECURITY_HEADERS.items():
        response.headers[name] = value


# ── a brake on repeat submissions ──────────────────────────────────────
#
# In-process and best effort, and said so out loud in the settings' comments.
# It stops the same person submitting the same form fifty times; it is not a
# defence against anything distributed, and it forgets everything on restart.

_recent: dict[str, deque[float]] = {}


def rate_limited(key: str, *, limit: int, window: float = 3600.0) -> bool:
    now = time.monotonic()
    seen = _recent.setdefault(key, deque())
    while seen and now - seen[0] > window:
        seen.popleft()
    if len(seen) >= limit:
        return True
    seen.append(now)
    if len(_recent) > 10_000:
        # Unbounded growth on a public endpoint is a slow memory leak with a
        # public trigger. Drop the coldest entries rather than grow for ever.
        for stale in [k for k, v in _recent.items() if not v or now - v[-1] > window][:5_000]:
            _recent.pop(stale, None)
    return False


def client_address(request: Request) -> str:
    """Who is asking, as far as a proxy lets us tell."""
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    return forwarded or (request.client.host if request.client else "unknown")


# ── CORS that leaves the website's door alone ──────────────────────────

#: Paths the credentialed CORS middleware does not touch. Only the website's
#: contact form: the careers paths are also fetched by the ERP's own frontend,
#: from an allowlisted origin, and keep the middleware's answer for it.
UNCREDENTIALED_PREFIXES: Final[tuple[str, ...]] = ("/website/",)


class ErpCORS(CORSMiddleware):
    """The ERP's CORS, minus the paths a stranger's browser posts to.

    The middleware is configured with ``allow_credentials`` for the session
    cookie, and on a simple request from *any* origin it adds
    ``Access-Control-Allow-Credentials: true`` — whether or not the origin is
    allowed. Beside ``Access-Control-Allow-Origin: *`` a browser will not honour
    it, but it is not what the public door means to say, so those paths answer
    with ``harden``'s headers alone. They take simple posts only, so no
    preflight ever needs the middleware either.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"].startswith(UNCREDENTIALED_PREFIXES):
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)
