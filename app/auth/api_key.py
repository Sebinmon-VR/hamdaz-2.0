"""Letting a service call this API without a browser session.

A session cookie identifies a person. Some callers are not people — a frontend
service, an automation, a scheduled job — and they need a different door. This
is that door: an ``X-API-Key`` header carrying ``HAMDAZ_API_KEY``.

Three properties this deliberately has:

**It is off unless configured.** An unset key authenticates nobody. The check is
``if not expected`` first, so a blank environment variable can never be matched
by a blank header — the failure mode that turns a missing config into an open
API.

**It is compared in constant time.** ``==`` on a secret leaks its prefix through
timing, and a key that can be discovered one byte at a time is not a key.

**It does not become a user.** The holder gets machine access, not somebody's
identity. Anything that must be attributed to a person — and any deletion —
still requires a session, which is why ``CurrentUser`` remains the default and
this is only accepted where a machine caller genuinely makes sense.
"""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from app.core.config import Settings, get_settings

HEADER = "X-API-Key"


def has_api_key(request: Request, settings: Settings) -> bool:
    """Whether this request carries the configured machine key."""
    expected = settings.hamdaz_api_key
    if not expected:
        # Not configured means header auth is disabled, not that anything goes.
        return False
    presented = request.headers.get(HEADER)
    if not presented:
        return False
    return hmac.compare_digest(presented, expected)


async def require_api_key(
    request: Request, settings: Annotated[Settings, Depends(get_settings)]
) -> None:
    if not has_api_key(request, settings):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"A valid {HEADER} header is required",
            headers={"WWW-Authenticate": HEADER},
        )


ApiKeyRequired = Annotated[None, Depends(require_api_key)]
