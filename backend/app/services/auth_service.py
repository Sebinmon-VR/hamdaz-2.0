"""Turning a verified Microsoft identity into a Hamdaz user.

The legacy onboarding flow wrote a row to a OneDrive spreadsheet and asked the user to pick
their own role from a dropdown. Here, signing in never grants access: a first-time user is
created with status ``invited`` and no memberships, and an admin decides what they can do.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.oidc import OIDCUser
from app.models.identity import User, UserStatus
from app.models.platform import AuditAction
from app.services import audit_service

logger = get_logger(__name__)


async def upsert_from_oidc(session: AsyncSession, identity: OIDCUser) -> tuple[User, bool]:
    """Find or create the user behind a verified sign-in. Returns ``(user, created)``.

    Matching is by Entra object ID first and email second. Object ID is stable across email
    changes; email is the fallback for a user pre-created by an admin invitation who has not
    signed in yet.
    """
    user = await session.scalar(
        select(User).where(User.azure_object_id == identity.object_id)
    )

    if user is None:
        user = await session.scalar(select(User).where(User.email == identity.email.lower()))
        if user is not None and user.azure_object_id is None:
            # An invited user signing in for the first time: bind their directory identity.
            user.azure_object_id = identity.object_id

    created = False
    if user is None:
        user = User(
            azure_object_id=identity.object_id,
            email=identity.email.lower(),
            display_name=identity.display_name,
            # Signing in is not authorization. An admin grants that.
            status=UserStatus.INVITED,
            joined_at=datetime.now(UTC),
        )
        session.add(user)
        await session.flush()
        created = True
        logger.info("auth.user_created", email=user.email, user_id=str(user.id))
    else:
        if identity.display_name and user.display_name != identity.display_name:
            user.display_name = identity.display_name

    user.last_login_at = datetime.now(UTC)
    await session.flush()

    await audit_service.record(
        session,
        action=AuditAction.CREATE if created else AuditAction.LOGIN,
        entity_type="user",
        entity_id=user.id,
        actor_id=user.id,
        after={"email": user.email, "status": user.status, "created": created},
    )

    return user, created


async def get_active_user(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    user = await session.scalar(select(User).where(User.id == user_id))
    if user is None or user.status is not UserStatus.ACTIVE:
        return None
    return user
