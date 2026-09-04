"""Turning someone from the org directory into a local user row.

Until now a ``users`` row appeared only when that person signed in, which made
the obvious administrative task impossible: you could not give a role to someone
who had not logged in yet. Setting up an organisation happens *before* people
arrive, not after.

So an admin action naming a person in the directory materialises them here on
demand. The row carries their Entra object id, which is what makes it the real
person rather than a placeholder that would collide at their first sign-in.

The team module needs exactly the same thing when adding members, which is why
this lives beside the directory rather than inside roles.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.directory.graph import GraphError, OrgUser
from app.models.user import User


class UserNotResolvableError(Exception):
    """The identifier matched no local user and nobody in the directory."""


def _user_from_org(person: OrgUser) -> User:
    return User(
        entra_object_id=person.object_id,
        # Accounts with no mailbox have no mail; the UPN is always present and
        # is what they sign in with.
        email=person.email or person.user_principal_name,
        display_name=person.display_name,
        # Provisioned ahead of their first sign-in, so is_active follows Entra.
        is_active=person.account_enabled,
    )


async def resolve_user(session: AsyncSession, directory, identifier: str) -> User:
    """Find the local user for ``identifier``, provisioning them if necessary.

    Accepts either a local ``users.id`` or an Entra object id. Both are UUIDs and
    cannot be told apart by shape, so they are tried in turn — local first, since
    that needs no network call.
    """
    key = str(identifier)

    # Only a users.id has to parse as a UUID. The object-id lookup below is a
    # plain string comparison, so it must not sit behind that check.
    try:
        as_uuid: uuid.UUID | None = uuid.UUID(key)
    except (ValueError, AttributeError):
        as_uuid = None

    if as_uuid is not None:
        local = await session.get(User, as_uuid)
        if local is not None:
            return local

    known = await session.scalar(select(User).where(User.entra_object_id == key))
    if known is not None:
        return known

    if as_uuid is None:
        # Entra object ids are UUIDs, so this cannot name anyone in the directory.
        raise UserNotResolvableError(f"{key!r} is not a valid id")

    # Not local under either id. If the directory knows them, they are a real
    # colleague who simply has not signed in yet.
    try:
        person = await directory.get_user(key)
    except GraphError as exc:
        raise UserNotResolvableError(
            f"No user with id {key}. It is neither a local user nor anyone "
            f"in the organisation directory."
        ) from exc

    user = _user_from_org(person)
    session.add(user)
    await session.flush()
    return user
