"""Turning a verified Microsoft identity into a row in ``users``."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.oidc import EntraIdentity
from app.models.user import User


async def upsert_user(session: AsyncSession, identity: EntraIdentity) -> User:
    """Find or create the user behind a verified sign-in, and stamp the login.

    Lookup is by Entra object id, not email — email is mutable and reusable, so
    matching on it merges two people who happen to inherit the same address.
    """
    user = await session.scalar(
        select(User).where(User.entra_object_id == identity.object_id)
    )

    if user is None:
        # No object-id match. An existing row with this email is the same person
        # seen before their object id was recorded, so adopt it rather than
        # creating a duplicate that would violate the unique index anyway.
        user = await session.scalar(select(User).where(User.email == identity.email))
        if user is not None:
            user.entra_object_id = identity.object_id

    if user is None:
        user = User(
            entra_object_id=identity.object_id,
            email=identity.email,
            display_name=identity.display_name,
        )
        session.add(user)

    # Entra is authoritative for both, so let a rename there flow through.
    user.email = identity.email
    user.display_name = identity.display_name
    user.last_login_at = datetime.now(UTC)

    await session.flush()  # populate server-generated id before we sign a cookie with it
    return user
