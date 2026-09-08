r"""Development entrypoint.

Raw docstring on purpose: it spells out a Windows path below, and ``\S`` in a
normal string is an invalid escape that Python warns about on every start.

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
from dotenv import load_dotenv

import app  # noqa: F401 — sets the Windows event-loop policy on import

# ``.env`` wins over the ambient environment — here, and only here.
#
# pydantic-settings gives an OS environment variable precedence over the same
# key in ``.env``. That is correct on Azure, where the App Service settings
# *are* the configuration and no ``.env`` is deployed. Locally it is backwards:
# a key exported into a shell once, or set machine-wide months ago, silently
# beats the file being edited. The failure that produces names the symptom and
# not the cause — a rejected API key, or worse, a query against the wrong
# database — and the file looks correct the whole time you are staring at it.
#
# So the *development entrypoint* lets the file win, and nothing else does.
# Tests set their own environment before importing Settings and never import
# this module, so they keep pointing at ``hamdaz_test``. Production runs
# ``uvicorn app.main:app`` directly, so it keeps the normal precedence and the
# platform stays in charge. Putting this in ``app/core/config.py`` instead would
# reverse both of those, which is why it is not there.
load_dotenv(override=True)


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
