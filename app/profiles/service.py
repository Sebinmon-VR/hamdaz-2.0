"""Assembling a user profile, and taking one apart.

Loading fans the registered sections out concurrently, each on its own session.
With the database ~310 ms away, four sections in sequence would cost four round
trips of latency for work that has no ordering between the parts.

Purging is the opposite: one session, one transaction, all sections or none.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.user import User
from app.profiles.registry import LoadContext, PurgeContext, Section, purgeable, resolve
from app.roles.catalogue import SUPER_ADMIN
from app.roles.service import count_super_admins, global_role_keys


class ProfileError(Exception):
    """A profile operation was refused. Safe to show a user."""


async def _run_section(
    section: Section,
    user: User,
    factory: async_sessionmaker[AsyncSession],
    directory: Any,
) -> tuple[str, Any, str | None, int]:
    """Load one section. Returns (key, data, error, elapsed_ms).

    A section that fails does not fail the profile — the rest is still useful,
    and the caller is told which part is missing and why.
    """
    started = time.monotonic()
    try:
        async with factory() as session:
            data = await section.load(
                LoadContext(user=user, session=session, directory=directory)
            )
        error = None
    except Exception as exc:  # noqa: BLE001 - one bad section must not sink the rest
        data, error = None, f"{type(exc).__name__}: {exc}"
    return section.key, data, error, int((time.monotonic() - started) * 1000)


async def load_profile(
    user: User,
    *,
    factory: async_sessionmaker[AsyncSession],
    directory: Any,
    include: list[str] | None = None,
    include_remote: bool = True,
) -> dict[str, Any]:
    sections = resolve(include, include_remote=include_remote)

    started = time.monotonic()
    results = await asyncio.gather(
        *(_run_section(s, user, factory, directory) for s in sections)
    )
    total_ms = int((time.monotonic() - started) * 1000)

    data: dict[str, Any] = {}
    errors: dict[str, str] = {}
    timings: dict[str, int] = {}
    for key, value, error, elapsed in results:
        data[key] = value
        timings[key] = elapsed
        if error:
            errors[key] = error

    return {
        "user_id": str(user.id),
        "email": user.email,
        "display_name": user.display_name,
        "sections": data,
        "errors": errors,
        "meta": {
            "requested": [s.key for s in sections],
            # Fanned out, so the total is the slowest section rather than the
            # sum — the number to watch if a section gets slow.
            "elapsed_ms": total_ms,
            "section_ms": timings,
        },
    }


async def _guard_destructive(
    session: AsyncSession, *, user: User, actor_id: uuid.UUID, purging: bool
) -> None:
    """Refuse the two ways this operation locks people out.

    Both apply to reset and purge alike: stripping a super admin's roles is just
    as final as deleting them if they were the last one.
    """
    if user.id == actor_id:
        raise ProfileError(
            "You cannot reset or delete your own account — ask another admin"
        )

    keys = await global_role_keys(session, user.id)
    if SUPER_ADMIN in keys and await count_super_admins(session) <= 1:
        raise ProfileError(
            "That is the last super admin — grant super admin to someone else first"
        )
    # Belt and braces: a super admin should be demoted deliberately before
    # their account is destroyed, not swept away as a side effect.
    if purging and SUPER_ADMIN in keys:
        raise ProfileError("Revoke super admin before deleting this account")


async def reset_user(
    session: AsyncSession, *, user: User, actor_id: uuid.UUID
) -> dict[str, int]:
    """Strip every module's data from a user, keeping the account itself.

    The row survives so their identity, and anything referencing it, stays
    intact — they simply hold nothing any more.
    """
    await _guard_destructive(session, user=user, actor_id=actor_id, purging=False)

    removed: dict[str, int] = {}
    for section in purgeable():
        assert section.purge is not None
        removed[section.key] = await section.purge(
            PurgeContext(user=user, session=session)
        )
    await session.flush()
    return removed


async def purge_user(
    session: AsyncSession, *, user: User, actor_id: uuid.UUID
) -> dict[str, int]:
    """Reset the user, then delete the account row itself.

    Their Entra account is untouched — this removes them from the ERP, not from
    the company. Signing in again would create a fresh, empty account.
    """
    await _guard_destructive(session, user=user, actor_id=actor_id, purging=True)

    removed: dict[str, int] = {}
    for section in purgeable():
        assert section.purge is not None
        removed[section.key] = await section.purge(
            PurgeContext(user=user, session=session)
        )

    await session.delete(user)
    await session.flush()
    removed["identity"] = 1
    return removed
