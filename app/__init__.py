"""Hamdaz ERP backend.

The event-loop fix below lives here, at package import, because it has to run
before *anything* creates a loop — uvicorn, Alembic and pytest all import ``app``
first, so this is the one hook all three share.

Windows defaults to ProactorEventLoop, which psycopg's async driver cannot use.
On Linux (Azure App Service) this is a no-op.
"""

from __future__ import annotations

import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
