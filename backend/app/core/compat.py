"""Platform compatibility shims.

Windows defaults asyncio to ``ProactorEventLoop``, which psycopg3 cannot use in async mode —
it raises ``InterfaceError`` on the very first connection attempt. Every async entry point
therefore selects a selector-based loop first.

This affects uvicorn, Alembic, the Celery tasks and the CLI equally, so the fix lives here
rather than being repeated (and eventually forgotten) at each call site.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Coroutine
from typing import Any

IS_WINDOWS = sys.platform == "win32"


def configure_event_loop_policy() -> None:
    """Make the default event loop policy psycopg-compatible.

    Call this at import time in any module that ends up owning an event loop. It is a no-op
    off Windows, and idempotent.
    """
    if not IS_WINDOWS:
        return

    policy_cls = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
    if policy_cls is None:  # pragma: no cover - non-Windows, or a future removal
        return

    if isinstance(asyncio.get_event_loop_policy(), policy_cls):
        return

    asyncio.set_event_loop_policy(policy_cls())


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    """``asyncio.run`` on a loop psycopg can actually use.

    Preferred over :func:`configure_event_loop_policy` at one-shot call sites: passing an
    explicit ``loop_factory`` avoids mutating global state, and avoids the policy APIs that
    Python is in the process of deprecating.
    """
    if IS_WINDOWS:
        return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)
    return asyncio.run(coro)
