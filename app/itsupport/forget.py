"""Clear IT Support's chat with one person: a fresh start.

    uv run python -m app.itsupport.forget sebin@hamdaz.com

Stop the backend first: it holds the notes in memory and would write them
back. Three things, for that person only:

* the notes kept about them (``memory``) are emptied;
* nothing in the chat from before now is read back as the conversation
  (``Memory.forget_before``): Teams keeps the person's own messages, which
  only they can delete, so the cut is what makes the agent forget them;
* IT Support's own messages in the chat are deleted (Teams shows "This
  message has been deleted"). The person's messages are theirs to delete.

The task list, the ERP and anything made from the chat are not touched.
"""

from __future__ import annotations

import asyncio
import socket
import sys
from datetime import UTC, datetime

import httpx

from app.core.config import get_settings
from app.itsupport.graph import GraphError, ITSupportGraph
from app.itsupport.memory import Memory


def _backend_running() -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", 8000)) == 0


async def forget(email: str) -> None:
    settings = get_settings()
    Memory(settings.it_support_memory_file).forget(email, datetime.now(UTC))
    print(f"Notes about {email} emptied; the chat before now won't be read back.")
    async with httpx.AsyncClient(timeout=60) as http:
        graph = ITSupportGraph(settings, http)
        me = await graph.me()
        person = await graph.user(email)
        chat_id = await graph.chat_with(me["id"], person["id"])
        messages = await graph.every_message(chat_id)
        own = [m for m in messages if m.get("messageType") == "message"
               and not m.get("deletedDateTime")
               and ((m.get("from") or {}).get("user") or {}).get("id") == me["id"]]
        deleted = failed = 0
        for m in own:
            for attempt in range(4):
                try:
                    await graph.delete_own(chat_id, m["id"])
                    deleted += 1
                    break
                except GraphError as exc:
                    if "throttled" in str(exc) and attempt < 3:
                        await asyncio.sleep(5 * (attempt + 1))
                        continue
                    failed += 1
                    print(f"  could not delete {m['id']}: {exc}")
                    break
        theirs = sum(1 for m in messages if m.get("messageType") == "message"
                     and not m.get("deletedDateTime")
                     and ((m.get("from") or {}).get("user") or {}).get("id") == person["id"])
        print(f"IT Support's messages deleted: {deleted} of {len(own)}"
              + (f" ({failed} failed)" if failed else "")
              + f". {email}'s own messages left in Teams (theirs to delete): {theirs}.")


def main() -> None:
    if len(sys.argv) != 2 or "@" not in sys.argv[1]:
        sys.exit("usage: python -m app.itsupport.forget someone@hamdaz.com")
    if _backend_running():
        sys.exit("The backend is running on :8000; stop it first, or it writes the notes back.")
    asyncio.run(forget(sys.argv[1].strip().lower()))


if __name__ == "__main__":
    main()
