"""The daily asks, in Teams: the status reminder's and the follow-up's.

The user's shortlist (2026-11-18, phase 1): the two questions the team is
emailed every day, from their own mailbox to themselves, and mostly never
open (Rameesa answered 0 of 20, Haleema 0 of 27), are asked by IT Support in
Teams instead, numbered so a reply can pick the task ("1 submitted"):

* **status**, after the status reminder has run (09:30 IST): the tasks it
  just asked about, due within its window and still open;
* **reason**, after the follow-up's daily batch (15:00 IST): the tasks past
  their BCD and not submitted that it just asked about.

The same rows the emails come from (``StatusReminder``, ``TaskFollowup``), so
the managers' 18:00 report and the web forms see the answers. The emails keep
going: this is in addition, until the user switches them off. Rules write the
message (no model): it's a list with numbers, and it must be exact. Once a day
per person and kind (``sent.Ledger``); people away are skipped; the morning
brief is not sent while these are on (they'd say the same thing twice).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Final

from sqlalchemy import select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.followups import service as fu
from app.itsupport import when
from app.itsupport.due import short
from app.itsupport.status import Asked, Item
from app.models.followup import FollowupSettings, FollowupStatus, TaskFollowup
from app.models.status_reminder import ReminderStatus, StatusReminder, StatusReminderSettings
from app.reminders import service as reminders

STATUS: Final = "status"
REASON: Final = "reason"

_OPENERS: Final = {
    STATUS: ("Morning {name}, where do these stand?",
             "Hi {name}, quick check on these:",
             "{name}, how are these looking?",
             "Morning {name}, a quick one on these:"),
    REASON: ("Hi {name}, these went past their BCD and aren't marked submitted:",
             "{name}, these are past their BCD with no submitted mark:",
             "Hey {name}, a couple past their BCD and still open:"),
}


@dataclass(frozen=True, slots=True)
class Row:
    task_id: str
    title: str
    due_at: datetime
    status: str | None
    has_remarks: bool


def today(fs: FollowupSettings, now: datetime) -> date:
    return fu.local_day(fs, now)


def status_ready(fs: FollowupSettings, rs: StatusReminderSettings, now: datetime) -> bool:
    """The status reminder has run today, so today's rows are there."""
    day = today(fs, now)
    return bool(rs.enabled and rs.last_run_on == day
                and now >= reminders.ask_moment(fs, rs, day))


def reason_ready(fs: FollowupSettings, now: datetime) -> bool:
    """The follow-up's daily batch has run today."""
    day = today(fs, now)
    return bool(fs.enabled and fs.ask_mode == "daily" and fs.ask_last_run_on == day
                and now >= fu.ask_moment(fs, day))


def _day_start(fs: FollowupSettings, day: date) -> datetime:
    return datetime.combine(day, time(0, 0), tzinfo=fu._settings_zone(fs)).astimezone(UTC)


async def rows(session: AsyncSession, kind: str, user_id, fs: FollowupSettings,
               day: date, *, trials: bool = False) -> list[Row]:
    """The person's questions of this kind made today and still waiting.
    ``trials``: also those made with "Try it" (no team), for a trial."""
    since = _day_start(fs, day)
    if kind == STATUS:
        found = (await session.scalars(
            select(StatusReminder).where(StatusReminder.assignee_id == user_id,
                                         StatusReminder.status == ReminderStatus.PENDING,
                                         true() if trials else
                                         StatusReminder.team_id.is_not(None),
                                         StatusReminder.created_at >= since)
            .order_by(StatusReminder.due_at))).all()
        return [Row(r.task_id, r.task_title, r.due_at, r.status_at_ask,
                    bool(r.remarks_at_ask)) for r in found]
    found = (await session.scalars(
        select(TaskFollowup).where(TaskFollowup.assignee_id == user_id,
                                   TaskFollowup.status == FollowupStatus.PENDING,
                                   true() if trials else TaskFollowup.team_id.is_not(None),
                                   TaskFollowup.created_at >= since)
        .order_by(TaskFollowup.due_at))).all()
    return [Row(r.task_id, r.task_title, r.due_at, r.status_at_ask, False) for r in found]


def message(kind: str, first_name: str, found: list[Row], now: datetime, *,
            risks: dict[str, str] | None = None) -> tuple[str, Asked]:
    """The ask, and what its numbers stand for."""
    risks = risks or {}
    openers = _OPENERS[kind]
    lines = [openers[now.toordinal() % len(openers)].format(name=first_name)]
    items = []
    for n, row in enumerate(found, start=1):
        verb = "closes" if row.due_at > now else "closed"
        line = (f"{n}. {row.task_id} · {short((row.title or '').strip(), 55)}, {verb} "
                f"{when.day_and_time(row.due_at, now)}")
        if kind == STATUS and row.status:
            line += f" · {row.status}"
        if risks.get(row.task_id):
            line += f" ⚠ {risks[row.task_id]}"
        lines.append(line)
        items.append(Item(n, row.task_id, row.title))
    if kind == STATUS:
        lines.append('Reply like "1 submitted" or "2 on hold, waiting for price".')
    else:
        hint = 'What held them up? Reply like "1 waiting for the supplier\'s price", or ' \
               '"1 submitted" if it went in.'
        lines.append(hint if len(found) > 1 else hint.replace("them", "it"))
    return "\n".join(lines), Asked(kind, items, now)
