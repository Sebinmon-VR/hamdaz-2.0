"""Short in-process caches over the two things every turn reads first.

This database is roughly a third of a second away. A turn that answers "hi"
was spending about three and a half seconds of that before OpenAI was called
at all: five or six queries to load the assistant's configuration, and another
five or six to work out what the person is allowed to see. Both answers are the
same for the whole minute either side of the question, so both are cached.

**The configuration cache is exact.** Settings, model prices, policies and
access rules change only when a super admin edits them, and every one of those
edits goes through this module's own router, which drops the cache on the way
out. The TTL is a backstop for the case this process did not make the change —
a second instance behind a load balancer, or a row edited directly.

**The permission cache is deliberately allowed to be stale**, and that is safe
for one reason worth stating plainly: it decides only which tools the model is
*shown*. Every tool call still travels the real route and meets the real guard
with the caller's own session, so a person whose access was revoked ten seconds
ago can still be offered a tool and will still be refused when it runs. The
cache can cost somebody a confusing refusal; it cannot grant anybody anything.

Both are per process and are lost on restart, which is correct: they are a
latency device, not a source of truth.
"""

from __future__ import annotations

import time
import uuid
from typing import Final

from app.assistant.policy import Actor
from app.assistant.service import Snapshot

#: The configuration is invalidated explicitly on every admin write, so this
#: only has to cover changes made by another process.
CONFIG_TTL_SECONDS: Final = 60

#: Short, because the cost of being wrong is a person seeing a tool they can no
#: longer use and being refused when they try it. Long enough that the several
#: turns of one conversation share a single lookup.
ACTOR_TTL_SECONDS: Final = 30

#: A bound so a large organisation cannot grow this without limit. Small: the
#: people using the assistant in any half minute are few.
_MAX_ACTORS: Final = 256


class ConfigCache:
    """The assistant's configuration, shared by everyone."""

    def __init__(self, ttl_seconds: int = CONFIG_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._entry: tuple[float, Snapshot] | None = None

    def get(self) -> Snapshot | None:
        if self._entry is None:
            return None
        stored_at, snapshot = self._entry
        if time.monotonic() - stored_at >= self._ttl:
            return None
        return snapshot

    def put(self, snapshot: Snapshot) -> Snapshot:
        self._entry = (time.monotonic(), snapshot)
        return snapshot

    def invalidate(self) -> None:
        """Called after any change to settings, models, policies or rules."""
        self._entry = None


class ActorCache:
    """One person's roles, teams and module access."""

    def __init__(self, ttl_seconds: int = ACTOR_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._entries: dict[uuid.UUID, tuple[float, Actor]] = {}

    def get(self, user_id: uuid.UUID) -> Actor | None:
        entry = self._entries.get(user_id)
        if entry is None:
            return None
        stored_at, actor = entry
        if time.monotonic() - stored_at >= self._ttl:
            del self._entries[user_id]
            return None
        return actor

    def put(self, user_id: uuid.UUID, actor: Actor) -> Actor:
        if len(self._entries) >= _MAX_ACTORS:
            # Oldest first. Cheap at this size and it keeps the busy people in.
            oldest = min(self._entries, key=lambda key: self._entries[key][0])
            del self._entries[oldest]
        self._entries[user_id] = (time.monotonic(), actor)
        return actor

    def invalidate(self, user_id: uuid.UUID | None = None) -> None:
        if user_id is None:
            self._entries.clear()
        else:
            self._entries.pop(user_id, None)
