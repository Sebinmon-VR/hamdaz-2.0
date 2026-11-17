"""What IT Support remembers about each person, between conversations.

The chat itself is short-term memory: the last IT_SUPPORT_HISTORY_MESSAGES
messages from Teams. This is the long-term part, like a colleague who
remembers last week: a few short notes per person (what they work on, what's
still open between you, what was promised, how they like answers), kept up to
date by the model once a conversation goes quiet
(IT_SUPPORT_MEMORY_IDLE_MINUTES), and given to every reply.

Kept in a local file on this machine (IT_SUPPORT_MEMORY_FILE, gitignored) for
the laptop trial; production would need a table.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from app.core.llm import Spoken, TextModel

logger = logging.getLogger("hamdaz.itsupport")

#: Notes longer than this are cut, so the instructions stay small.
MAX_CHARS: Final = 1500

UPDATE: Final = """\
You keep short notes about a colleague you chat with as IT Support at Hamdaz, so you
remember them next time, the way a teammate would.

Your notes so far:
{notes}

The latest conversation (oldest first, "Me" is you):
{chat}

Write the updated notes. Keep what will matter later: what they work on, anything still
open or unresolved, what you said you'd do or they said they'd come back about, what they
said they liked or didn't (tone, length: only what THEY said, never how you yourself
replied), and facts they told you. Date things that are time-bound (today is {today}).
Drop what's finished and no longer useful, small talk, anything you can't stand behind, and
open items nobody has mentioned for a week. At most 12 short lines starting with "- ".
Only the notes."""


class Memory:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        try:
            self._rows: dict[str, dict[str, Any]] = json.loads(self._path.read_text("utf-8"))
        except (OSError, ValueError):
            self._rows = {}

    def notes(self, email: str) -> str:
        return (self._rows.get(email.casefold()) or {}).get("notes", "")

    def last_seen(self, email: str) -> str | None:
        """The newest message already folded into the notes."""
        return (self._rows.get(email.casefold()) or {}).get("upto")

    def forget_before(self, email: str) -> datetime | None:
        """Messages older than this are not theirs to remember: the chat was
        cleared (``app.itsupport.forget``). Teams keeps the person's own
        messages, which only they can delete, so the cut is kept here."""
        value = (self._rows.get(email.casefold()) or {}).get("forget_before")
        return datetime.fromisoformat(value) if value else None

    def forget(self, email: str, now: datetime) -> None:
        """No notes, and nothing before ``now`` read back as the chat."""
        self._rows[email.casefold()] = {"notes": "", "forget_before": now.isoformat(),
                                        "updated": now.isoformat()}
        self._write()

    def keep(self, email: str, notes: str, upto: str, now: datetime) -> None:
        row = self._rows.get(email.casefold()) or {}
        self._rows[email.casefold()] = {
            "notes": notes[:MAX_CHARS], "upto": upto, "updated": now.isoformat(),
            **({"forget_before": row["forget_before"]} if row.get("forget_before") else {}),
        }
        self._write()

    def _write(self) -> None:
        try:
            self._path.write_text(json.dumps(self._rows, indent=1, ensure_ascii=False), "utf-8")
        except OSError as exc:
            logger.warning("it support: could not keep the notes: %s", exc)


async def update(
    model: TextModel, *, notes: str, chat: list[dict[str, str]], now: datetime, order: str = "",
) -> str | None:
    """The notes with this conversation folded in; None when no model answered."""
    lines = "\n".join(
        f"{'Me' if turn['role'] == 'assistant' else 'Them'}: {turn['content'][:600]}"
        for turn in chat
    )
    spoken = Spoken()
    async for _ in model.chat(
        system="You write brief, factual notes. No preamble.",
        messages=[{"role": "user", "content": UPDATE.format(
            notes=notes or "(none yet)", chat=lines, today=f"{now:%a %d %b %Y}")}],
        spoken=spoken, order=order, max_tokens=500, label="it support memory",
    ):
        pass
    text = spoken.text.strip()
    return text or None
