"""The registry every module plugs its user data into.

The problem this solves: as modules pile up, "everything we know about a person"
gets scattered across them, and both the aggregate view and the "reset this user"
operation have to be edited each time. Two lists to keep in step is how one of
them quietly goes stale — and the one that goes stale is the purge, which means
leftover rows nobody knows about.

So a module contributes a single :class:`Section` that knows how to *load* its
slice and how to *purge* it. Adding a module is one entry; the aggregate endpoint
and the reset both pick it up automatically.

Sections are loaded concurrently, each with its own database session, because an
``AsyncSession`` cannot be shared across concurrent tasks and this database is a
third of a second away — running four sections in sequence would be four times
the latency for no reason.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User


@dataclass(slots=True)
class LoadContext:
    """What a loader is given. ``session`` is private to this section."""

    user: User
    session: AsyncSession
    directory: Any


@dataclass(slots=True)
class PurgeContext:
    """What a purger is given.

    Unlike loading, purging shares one session across every section so the whole
    reset commits or rolls back as a unit. A half-purged user is worse than an
    un-purged one.
    """

    user: User
    session: AsyncSession


@dataclass(frozen=True, slots=True)
class Section:
    key: str
    label: str
    #: Returns this section's data. Anything JSON-serialisable, or None.
    load: Callable[[LoadContext], Awaitable[Any]]
    #: Removes this section's data, returning how many rows went. None means the
    #: section owns nothing deletable — a live directory record, for instance,
    #: which belongs to Entra and must never be touched from here.
    purge: Callable[[PurgeContext], Awaitable[int]] | None = None
    #: Loaders that reach the network are marked so a caller can skip them when
    #: only local data is wanted.
    remote: bool = False
    #: Excluded from the default response; must be asked for by name.
    heavy: bool = False


#: Insertion-ordered, so sections appear in the response in registration order.
_SECTIONS: dict[str, Section] = {}


def register(section: Section) -> Section:
    if section.key in _SECTIONS:
        raise ValueError(f"profile section {section.key!r} is already registered")
    _SECTIONS[section.key] = section
    return section


def all_sections() -> list[Section]:
    return list(_SECTIONS.values())


def section_keys() -> list[str]:
    return list(_SECTIONS)


def resolve(keys: list[str] | None, *, include_remote: bool = True) -> list[Section]:
    """The sections to run for this request.

    ``None`` means "everything not marked heavy". An explicit list is honoured
    exactly, heavy sections included — asking for one by name is the opt-in.
    """
    if keys is None:
        chosen = [s for s in _SECTIONS.values() if not s.heavy]
    else:
        unknown = [k for k in keys if k not in _SECTIONS]
        if unknown:
            raise KeyError(
                f"unknown section(s): {', '.join(sorted(unknown))}. "
                f"Available: {', '.join(sorted(_SECTIONS))}"
            )
        chosen = [_SECTIONS[k] for k in keys]

    if not include_remote:
        chosen = [s for s in chosen if not s.remote]
    return chosen


def purgeable() -> list[Section]:
    return [s for s in _SECTIONS.values() if s.purge is not None]
