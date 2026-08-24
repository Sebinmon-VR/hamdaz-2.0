"""Session tokens.

Microsoft SSO stays — Azure AD remains the identity provider — but *authorization* moves
into Postgres. After the OIDC callback we mint our own short-lived JWT carrying nothing but
the user ID and a session ID. Permissions are deliberately **not** in the token: a role
change must take effect on the next request, not whenever the token happens to expire.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from jose import JWTError, jwt

from app.core.config import Settings
from app.core.errors import AuthenticationError

TOKEN_TYPE: Final = "access"  # noqa: S105 - a JWT claim discriminator, not a credential


def create_access_token(
    *,
    user_id: uuid.UUID,
    settings: Settings,
    session_id: str | None = None,
    expires_in: int | None = None,
) -> tuple[str, datetime]:
    """Return ``(token, expires_at)``."""
    now = datetime.now(UTC)
    ttl = expires_in if expires_in is not None else settings.jwt_ttl_seconds
    expires_at = now + timedelta(seconds=ttl)

    claims: dict[str, Any] = {
        "sub": str(user_id),
        "typ": TOKEN_TYPE,
        "sid": session_id or uuid.uuid4().hex,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, expires_at


def decode_access_token(token: str, settings: Settings) -> dict[str, Any]:
    try:
        claims: dict[str, Any] = jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
    except JWTError as exc:
        raise AuthenticationError("Session token is invalid or has expired.") from exc

    if claims.get("typ") != TOKEN_TYPE:
        raise AuthenticationError("Session token is of the wrong type.")
    if not claims.get("sub"):
        raise AuthenticationError("Session token is missing a subject.")
    return claims


def user_id_from_token(token: str, settings: Settings) -> uuid.UUID:
    claims = decode_access_token(token, settings)
    try:
        return uuid.UUID(str(claims["sub"]))
    except (ValueError, KeyError) as exc:
        raise AuthenticationError("Session token subject is not a valid user ID.") from exc
