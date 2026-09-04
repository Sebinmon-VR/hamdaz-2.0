"""Development entrypoint.

Exists because of Windows. uvicorn installs its own event loop at startup and
overrides any policy set at import time, and on Windows that loop is
ProactorEventLoop — which psycopg's async driver refuses to run on. Passing
``loop="none"`` tells uvicorn to use the loop it is already inside, so the
selector loop chosen in ``app/__init__.py`` survives.

**There is no auto-reload here, and the reason is worth recording** so nobody
adds it back expecting it to work. ``uvicorn.Config(reload=True)`` is inert on
its own — only ``uvicorn.run()`` acts on it, by starting a supervisor that
spawns a child process. That was tried. On Windows the supervisor detected the
change and logged "Reloading...", but the replacement child never came up, while
the original kept serving: the server looked healthy and ran stale code, which is
worse than not reloading at all. The spawned child does not inherit the event
loop arrangement above, which is the whole reason this file exists.

So: **after changing anything under ``app/``, stop this and start it again.**
Ctrl+C, then ``.venv\Scripts\python.exe run.py``.

On Linux (Azure App Service) none of this applies and the standard command is
fine, reload included:

    python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio

import uvicorn

import app  # noqa: F401 — sets the Windows event-loop policy on import


def main() -> None:
    server = uvicorn.Server(
        uvicorn.Config(
            "app.main:app",
            host="127.0.0.1",
            port=8000,
            loop="none",
            # Deliberately off — see the module docstring. A reload that
            # silently fails to restart is a trap, not a convenience.
            reload=False,
            log_level="info",
        )
    )
    asyncio.run(server.serve())


if __name__ == "__main__":
    main()
