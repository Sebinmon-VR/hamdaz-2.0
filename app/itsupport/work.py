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
from typing import TYPE_CHECKING, Final
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.followups import service as fu
from app.itsupport import when
from app.proposals.sharepoint import ProposalTask, SharePointProposals
from app.reminders.service import is_done

if TYPE_CHECKING:
    from app.itsupport.tasklist import Member

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
    read = f"(Read from the Proposals list {when.from_now(work.read_at, now)}, at " \
           f"{when.both(work.read_at)}.)"
    soon = open_soon(work, now)
    if not soon:
        return f"(No open tasks due in the next week.) {read}"
    lines = [f"Their open tasks, due from 2 days ago to a week ahead {read}"]
    for due, task in soon:
        lines.append(f"- {task.id}: {(task.title or '').strip()} — "
                     + "; ".join(_bits(task, due, work.held, now, remarks=120)))
    return "\n".join(lines)


def _bits(task: ProposalTask, due: datetime, held: set[str], now: datetime, *,
          remarks: int) -> list[str]:
    """One task's line after its title: BCD, status, submission, end user, remarks."""
    if is_done(task):
        bits = [f"DONE, BCD {when.said(due, now)}"]
    else:
        bits = [unset(task, now)] if task.id in held else [
            f"BCD {when.said(due, now)}" + (" (PASSED, not marked done)" if due < now else "")]
    bits.append(f"status {task.status or 'not set'}")
    if task.submission_status:
        bits.append(f"submission {task.submission_status}")
    if task.end_user:
        bits.append(f"end user {task.end_user[:60]}")
    if task.remarks:
        bits.append(f"remarks: {' '.join(task.remarks.split())[:remarks]}")
    return bits


def unset(task: ProposalTask, now: datetime) -> str:
    """A task whose BCD is still the assignment time (the BCD check's
    placeholder): not due then, its real closing date not set yet."""
    created = when.parse_when(task.created_at)
    assigned = f"assigned {when.day_and_time(created, now)}, " if created else ""
    return f"{assigned}BCD not set yet (it still shows the assignment time; NOT due then)"


#: The team list in the context, at most this many (soonest BCD first).
TEAM_SHOWN_MAX: Final = 40


def describe_team(team: str, members: list[Member], held: set[str], *, read_at: datetime,
                  now: datetime) -> tuple[str, int]:
    """A team's open tasks, a line each with who holds it (and whether they're
    away), for someone who oversees the team; today's done ones too, marked,
    so it matches the morning's list. With it, how many tasks it has."""
    read = f"(read from the Proposals list {when.from_now(read_at, now)}, at " \
           f"{when.both(read_at)})"
    today = now.astimezone(UAE).date()
    items = []
    for member in members:
        for task in member.tasks:
            due = fu.due_of(task)
            if due is None or not (now - SHOWN_SINCE <= due <= now + SHOWN_UNTIL):
                continue
            if is_done(task) and due.astimezone(UAE).date() != today:
                continue
            name = member.full_name or member.name
            items.append((due, name + (f" (away: {member.away})" if member.away else ""), task))
    if not items:
        return f"The {team} team has no open tasks due from 2 days ago to a week ahead {read}.", 0
    items.sort(key=lambda item: item[0])
    lines = [f"The {team} team's open tasks, due from 2 days ago to a week ahead, and today's "
             f"done ones (marked DONE) {read}:"]
    for due, holder, task in items[:TEAM_SHOWN_MAX]:
        lines.append(f"- {task.id}: {(task.title or '').strip()[:90]} — {holder} — "
                     + "; ".join(_bits(task, due, held, now, remarks=80)))
    if len(items) > TEAM_SHOWN_MAX:
        lines.append(f"(and {len(items) - TEAM_SHOWN_MAX} more due later in the week: read "
                     "the ERP for those)")
    return "\n".join(lines), len(items)
