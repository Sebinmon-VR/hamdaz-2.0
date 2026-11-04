"""Which Proposals tasks have their folder in the Proposal Team Channel, and
which of their list attachments have been copied into it.

One row per task the folder step has dealt with — see
``app/proposals/folders.py``. It is what makes the step happen once per task: a
row with ``done_at`` set is never given a folder again, so a folder the team
renames or deletes afterwards is theirs to keep that way.

A table of its own rather than columns on ``proposal_index``: every reader of
the mirror selects that table's columns, so a column added there before its
migration ran would break all of them. Missing, this table breaks only the
folder step, which logs it and leaves everything else alone.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamped


class TaskFolder(Base, Timestamped):
    __tablename__ = "task_folders"

    #: The SharePoint list item id, as on ``proposal_index``.
    item_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    #: The title the folder was looked for (and named) by, as it was then.
    title: Mapped[str] = mapped_column(Text, nullable=False)
    #: The folder's name under the root, once known.
    folder: Mapped[str | None] = mapped_column(Text)
    #: Its drive item id. Files are copied in by this, so a folder the team
    #: renames still gets them, and one they delete is not made again.
    folder_id: Mapped[str | None] = mapped_column(String(64))
    web_url: Mapped[str | None] = mapped_column(Text)
    #: exact — a folder of that name was there already; number — one starting
    #: with the same event number was; made — this app made it.
    how: Mapped[str | None] = mapped_column(String(20))
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    tries: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    tried_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)

    # ── the task's list attachments, copied into the folder ────────────
    #: Every attachment dealt with, keyed by its name in lower case:
    #: ``{"name", "as", "how", "size", "url", "at"}``, where ``how`` is copied,
    #: already there (a file of that name was in the folder, left alone) or
    #: too large. A name in here is never copied again — so a copy the team
    #: deletes or moves stays deleted or moved.
    files: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    #: The task's Modified stamp when its attachments were last read. Adding
    #: an attachment moves Modified, which is how a new one is noticed.
    files_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    files_tried_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    files_error: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:
        return f"<TaskFolder {self.item_id} {self.how or 'pending'} {self.title[:40]!r}>"
