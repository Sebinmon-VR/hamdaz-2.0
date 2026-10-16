"""AI agents — paused.

The user's call (2026-10-17): agents are still being built, so they are off
for everyone for a while. The code stays; nothing of it runs. With ENABLED
False the app serves no /agents routes and starts no agent worker, so no
agent reads a task, drafts or sends anything, and the frontend hides every
trace of them (``AGENTS_ENABLED`` in its ``src/lib/features.ts``).

To bring them back: set this True here and ``AGENTS_ENABLED`` true in the
frontend, together.
"""

from typing import Final

ENABLED: Final = False
