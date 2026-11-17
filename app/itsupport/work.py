"""A person's work, as IT Support knows it: their tasks on the Proposals list.

Read every few minutes for each person it chats with (``worker``), so a reply
never waits on SharePoint. Two things use it: the due-today check-in
(``due``), and the chat, which is shown the person's open tasks so "can you
help with the Zebra one?" is understood.

Reads the list; never writes it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.followups import service as fu
from app.proposals.sharepoint import ProposalTask, SharePointProposals
from app.reminders.service import is_done

#: BCD times are UAE times ("BCD UAE Time"), and "today" is the UAE's.
UAE: Final = ZoneInfo("Asia/Dubai")
#: Part of the team works on India time, so a time is said in both.
IST: Final = ZoneInfo("Asia/Kolkata")
#: The chat is shown open tasks due from this long ago ...
SHOWN_SINCE: Final = timedelta(days=2)
#: ... to this far ahead, at most this many.
SHOWN_UNTIL: Final = timedelta(days=7)
SHOWN_MAX: Final = 15


@dataclass(slots=True)
class Work:
    tasks: list[ProposalTask] = field(default_factory=list)
    #: Tasks whose BCD is still the assignment-time placeholder.
    held: set[str] = field(default_factory=set)
    read_at: datetime | None = None


async def read(
    sharepoint: SharePointProposals,
    factory: async_sessionmaker[AsyncSession] | None,
    email: str,
) -> Work:
    lookup = await sharepoint.lookup_id_for(email)
    tasks = await sharepoint.tasks_assigned_to(lookup, limit=500) if lookup else []
    held: set[str] = set()
    if factory is not None and tasks:
        from app.bcd import service as bcd

        async with factory() as session:
            held = await bcd.held_ids(session, tasks)
    return Work(tasks, held, datetime.now(UTC))


def clock(moment: datetime, zone: ZoneInfo) -> str:
    return moment.astimezone(zone).strftime("%I:%M %p").lstrip("0")


def both(moment: datetime) -> str:
    """A time in both clocks the team uses: "5:00 PM UAE (6:30 PM IST)".

    The BCD is a UAE time; part of the team works on India time, so a bare
    "5 PM" is read two ways. Worked out here, never left to the model.
    """
    return f"{clock(moment, UAE)} UAE ({clock(moment, IST)} IST)"


def at(moment: datetime, now: datetime) -> str:
    """A due moment as the team says it: "today 5:00 PM UAE (6:30 PM IST)"."""
    local, today = moment.astimezone(UAE), now.astimezone(UAE).date()
    if local.date() == today:
        return f"today {both(moment)}"
    if local.date() == today + timedelta(days=1):
        return f"tomorrow {both(moment)}"
    return f"{local:%a %d %b} {both(moment)}"


def open_soon(work: Work, now: datetime) -> list[tuple[datetime, ProposalTask]]:
    out = []
    for task in work.tasks:
        due = fu.due_of(task)
        if due is None or is_done(task) or not (now - SHOWN_SINCE <= due <= now + SHOWN_UNTIL):
            continue
        out.append((due, task))
    return sorted(out, key=lambda pair: pair[0])[:SHOWN_MAX]


def describe(work: Work | None, now: datetime) -> str:
    """The person's open tasks, a line each, for the chat's instructions."""
    if work is None or work.read_at is None:
        return "(Their tasks couldn't be read just now.)"
    soon = open_soon(work, now)
    if not soon:
        return "(No open tasks due in the next week.)"
    lines = []
    for due, task in soon:
        bits = [f"BCD {at(due, now)}" + (" (OVERDUE)" if due < now else "")]
        if task.id in work.held:
            bits = ["BCD not set yet"]
        bits.append(f"status {task.status or 'not set'}")
        if task.end_user:
            bits.append(f"end user {task.end_user}")
        if task.remarks:
            bits.append(f"remarks: {' '.join(task.remarks.split())[:120]}")
        lines.append(f"- {(task.title or '').strip()} — " + "; ".join(bits))
    return "\n".join(lines)
