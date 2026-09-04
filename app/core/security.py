"""Signed, tamper-evident cookie payloads.

Both cookies this app sets — the session and the short-lived login state — are
JWTs signed with ``SESSION_SECRET``. They are signed, not encrypted: a user can
read their own cookie, but cannot alter it without detection. Nothing secret
goes inside one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

_ALGORITHM = "HS256"


class InvalidTokenError(Exception):
    """Signature failed, the token expired, or the audience did not match."""


def sign(payload: dict[str, Any], *, secret: str, ttl_minutes: int, audience: str) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            **payload,
            "aud": audience,
            "iat": now,
            "exp": now + timedelta(minutes=ttl_minutes),
        },
        secret,
        algorithm=_ALGORITHM,
    )


def verify(token: str, *, secret: str, audience: str) -> dict[str, Any]:
    try:
        return jwt.decode(
            token,
            secret,
            algorithms=[_ALGORITHM],
            audience=audience,
            options={"require": ["exp", "iat", "aud"]},
        )
    except jwt.PyJWTError as exc:
        # The caller only ever needs "this cookie is not usable".
        raise InvalidTokenError(str(exc)) from exc
