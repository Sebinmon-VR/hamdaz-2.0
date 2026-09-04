"""Dependencies that answer "who is making this request".

``current_user`` is what every future ERP module will depend on, so the rule it
enforces is deliberately narrow: a valid, unexpired session cookie belonging to
a user who is still active here.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.core.security import InvalidTokenError, verify
from app.models.user import User

SESSION_AUDIENCE = "hamdaz:session"

_UNAUTHENTICATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
)


async def current_user(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> User:
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        raise _UNAUTHENTICATED

    try:
        claims = verify(token, secret=settings.session_secret, audience=SESSION_AUDIENCE)
    except InvalidTokenError:
        # Expired, tampered with, or signed under a rotated secret. All three
        # mean the same thing to the caller: log in again.
        raise _UNAUTHENTICATED from None

    try:
        user_id = uuid.UUID(claims["sub"])
    except (KeyError, ValueError):
        raise _UNAUTHENTICATED from None

    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        # Deactivating someone takes effect on their next request, without
        # waiting for the cookie to expire.
        raise _UNAUTHENTICATED

    return user


CurrentUser = Annotated[User, Depends(current_user)]
