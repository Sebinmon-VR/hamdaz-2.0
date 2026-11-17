"""Due-today check-ins: IT Support messages a person first about a task due today.

The user's ask (2026-11-13): when somebody has a task whose BCD is today, IT
Support says so in Teams, the way a colleague would: "hey, saw you've got the
ADNOC cartridges RFQ closing at 1:30 PM today, need a hand with anything?"

What counts: a task on the Proposals list assigned to the person, its due
moment (the BCD, else the due date; ``followups.due_of``) **today in the
UAE**, still at least ``LEAD`` ahead, not Completed or Submitted, and not
held for a placeholder BCD (``bcd.held_ids``), the same rules the reminders
use. One message per person for all of the day's tasks, sent in office hours
on working days, and each task is asked about once a day: what was asked is
kept in a small file on this machine (IT_SUPPORT_DUE_ASKED_FILE), so a restart
does not ask again.

The message is written by the chat's model in the chat's own voice
(``talk.SYSTEM``), with a plain fallback when no model answers. After it,
the conversation is the ordinary chat: the check-in sits in the history, so a
reply to it is answered knowing what was asked.

Reads the Proposals list; never writes it. Off unless IT_SUPPORT_DUE_ENABLED.

Preview, sending nothing; then, only when told to, one real send::

    uv run python -m app.itsupport.due
    uv run python -m app.itsupport.due --day tomorrow
    uv run python -m app.itsupport.due --send haleema@hamdaz.com --day tomorrow
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo

from app.core.config import Settings
from app.core.llm import Spoken, TextModel
from app.followups import service as fu
from app.itsupport import talk
from app.itsupport.work import UAE, Work, both
from app.reminders.service import is_done

logger = logging.getLogger("hamdaz.itsupport")

#: Closer to the BCD than this, a "need a hand?" comes too late to help.
LEAD: Final = timedelta(minutes=20)
#: Asked-about records older than this are dropped from the file.
KEEP_DAYS: Final = 7
#: A task closing this soon after office opens can't be helped on the day
#: (Jasna's 6:30 AM UAE RFQ is 8:00 AM IST, before 9:00): it is asked about
#: the working day before.
EARLY: Final = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class DueTask:
    task_id: str
    title: str
    due_at: datetime  # UTC
    status: str | None
    #: "today", "tomorrow" or "on Monday", from when it is asked.
    when: str = "today"

    @property
    def at(self) -> str:
        """The time in both clocks: "5:00 PM UAE (6:30 PM IST)"."""
        return both(self.due_at)


# ── which tasks ────────────────────────────────────────────────────────


def day_word(day: date, today: date) -> str:
    if day == today:
        return "today"
    if day == today + timedelta(days=1):
        return "tomorrow"
    return f"on {day:%A}"


def due_on(work: Work, *, now: datetime, day: date | None = None) -> list[DueTask]:
    """Of a person's tasks, those due on ``day`` (UAE; today if not given),
    still ahead and still open."""
    today = now.astimezone(UAE).date()
    day = day or today
    out = []
    for task in work.tasks:
        due = fu.due_of(task)
        if due is None or due.astimezone(UAE).date() != day:
            continue
        if is_done(task) or task.id in work.held or due < now + LEAD:
            continue
        out.append(DueTask(task.id, (task.title or "").strip(), due, task.status,
                           day_word(day, today)))
    return sorted(out, key=lambda t: t.due_at)


def to_ask(work: Work, *, now: datetime, settings: Settings) -> list[DueTask]:
    """What to ask about now: today's, plus what closes before the next
    working day is really under way (before office opens + ``EARLY``), and
    anything on the days off in between."""
    zone, days, start, _ = _office(settings)
    today = now.astimezone(UAE).date()
    out = due_on(work, now=now)
    day = today + timedelta(days=1)
    for _ in range(7):
        if day.weekday() in days:
            break
        out += due_on(work, now=now, day=day)
        day += timedelta(days=1)
    cutoff = datetime.combine(day, start, tzinfo=zone) + EARLY
    out += [t for t in due_on(work, now=now, day=day) if t.due_at < cutoff]
    return sorted(out, key=lambda t: t.due_at)


# ── when ───────────────────────────────────────────────────────────────


def _clock(value: str, default: str) -> time:
    try:
        hour, minute = (int(x) for x in (value or default).split(":")[:2])
    except ValueError:
        hour, minute = (int(x) for x in default.split(":"))
    return time(hour, minute)


def _office(settings: Settings) -> tuple[ZoneInfo, set[int], time, time]:
    try:
        zone = ZoneInfo(settings.it_support_due_zone)
    except Exception:  # noqa: BLE001 - a bad name falls back to the team's
        zone = ZoneInfo("Asia/Kolkata")
    days = {int(d) for d in settings.it_support_due_days.split(",") if d.strip().isdigit()}
    start = _clock(settings.it_support_due_from, "09:00")
    end = _clock(settings.it_support_due_until, "17:00")
    return zone, days, start, end


def in_office_hours(settings: Settings, now: datetime) -> bool:
    zone, days, start, end = _office(settings)
    local = now.astimezone(zone)
    return local.weekday() in days and start <= local.time().replace(tzinfo=None) < end


# ── asked once ─────────────────────────────────────────────────────────


class Asked:
    """Which task was asked about for which UAE day, kept in a local file."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        try:
            self._rows: dict[str, str] = json.loads(self._path.read_text("utf-8"))
        except (OSError, ValueError):
            self._rows = {}

    @staticmethod
    def _key(task: DueTask) -> str:
        return f"{task.task_id}@{task.due_at.astimezone(UAE).date().isoformat()}"

    def new(self, tasks: list[DueTask]) -> list[DueTask]:
        return [t for t in tasks if self._key(t) not in self._rows]

    def mark(self, tasks: list[DueTask], now: datetime) -> None:
        for task in tasks:
            self._rows[self._key(task)] = now.isoformat()
        cutoff = (now - timedelta(days=KEEP_DAYS)).date()
        self._rows = {
            k: v for k, v in self._rows.items()
            if date.fromisoformat(k.rsplit("@", 1)[1]) >= cutoff
        }
        try:
            self._path.write_text(json.dumps(self._rows, indent=1), "utf-8")
        except OSError as exc:
            logger.warning("it support: could not keep who was asked: %s", exc)


# ── the message ────────────────────────────────────────────────────────

ASK: Final = """\
(Not a message from them: you're starting this chat yourself. They haven't written.)
You noticed they have {what} closing soon (bid closing times):

{lines}

Write the one message you'd send a colleague: say hi, mention what's due and when (the time
exactly as given, both UAE and IST, since part of the team works on India time), and ask
if they need a hand with it (finding suppliers or distributors, or anything else). One or
two short sentences, warm and casual, like a teammate who happened to notice, not a
reminder system. Call a task by its RFQ or tender number plus a few words of what it is,
never the whole title. Don't mention its status, the Proposals list or that you were told
to write this. Only the message itself."""


def short(title: str, limit: int = 60) -> str:
    return title if len(title) <= limit else title[: limit - 1].rstrip(" -,") + "…"


def fallback(first_name: str, tasks: list[DueTask]) -> str:
    if len(tasks) == 1:
        t = tasks[0]
        return (f"Hi {first_name}, I saw you have {short(t.title)} due {t.when} at {t.at}. "
                "Need any help with it?")
    listed = ", ".join(f"{short(t.title)} {t.when} at {t.at}" for t in tasks)
    return f"Hi {first_name}, I saw you have a few coming up: {listed}. Need a hand with any?"


async def write(
    model: TextModel, *, first_name: str, tasks: list[DueTask], now: datetime,
    order: str = "", context: str = "",
) -> Spoken:
    lines = "\n".join(f"- {t.title} (due {t.when} {t.at})" for t in tasks)
    what = "a task" if len(tasks) == 1 else f"{len(tasks)} tasks"
    spoken = Spoken()
    try:
        async for _ in model.chat(
            system=talk.system_for(first_name, now, context=context),
            messages=[{"role": "user", "content": ASK.format(
                what=what, lines=lines)}],
            spoken=spoken, order=order, max_tokens=200, label="it support due check-in",
        ):
            pass
    except Exception as exc:  # noqa: BLE001 - the plain message still goes
        logger.warning("it support: the check-in model failed: %s", exc)
        spoken = Spoken()
    text = spoken.text.strip().strip('"').strip()
    spoken.text = text or fallback(first_name, tasks)
    return spoken


# ── preview, and the one-off send the user asks for ────────────────────


async def _main(send_to: str | None, day_word: str, message: str | None = None) -> None:
    import httpx

    from app.core.config import get_settings
    from app.core.db import get_session_factory, init_engine
    from app.itsupport import settings as settings_mod
    from app.itsupport import work as work_mod
    from app.itsupport.graph import ITSupportGraph
    from app.proposals.sharepoint import SharePointProposals

    settings = get_settings().model_copy()
    init_engine(settings)
    logging.getLogger("sqlalchemy.engine.Engine").setLevel(logging.WARNING)  # DB echo
    # The settings page's people, hours and names, as the worker uses them.
    if (row := await settings_mod.load(get_session_factory(), settings)) is not None:
        settings_mod.apply(row, settings)
    now = datetime.now(UTC)
    day = now.astimezone(UAE).date() + timedelta(days=1)
    print(f"now: {now.astimezone(UAE):%a %d %b %H:%M} UAE · "
          f"{'all due tomorrow' if day_word == 'tomorrow' else 'what the worker asks now'}"
          f" · {'SENDING to ' + send_to if send_to else 'preview, nothing is sent'}")
    asked = Asked(settings.it_support_due_asked_file)
    async with httpx.AsyncClient(timeout=30) as http:
        sharepoint = SharePointProposals(settings, http)
        model = TextModel(settings, http)
        graph = ITSupportGraph(settings, http)
        people = [send_to] if send_to else [
            e.strip() for e in settings.it_support_allowed_emails.split(",") if e.strip() not in
            ("", "*")
        ]
        for email in people:
            work = await work_mod.read(sharepoint, get_session_factory(), email)
            if day_word == "tomorrow":
                tasks = due_on(work, now=now, day=day)
            else:
                tasks = to_ask(work, now=now, settings=settings)
            fresh = asked.new(tasks)
            print(f"\n-- {email}: {len(tasks)} to ask about, "
                  f"{len(tasks) - len(fresh)} already asked")
            for t in tasks:
                print(f"   {t.when:>9} {t.at:>28}  {t.status or '-':<12} {t.title[:70]}"
                      f"{'' if t in fresh else '   (already asked)'}")
            tasks = fresh
            if not tasks:
                continue
            person = await graph.user(email)
            first = talk.first_name(settings, email, person.get("displayName"))
            if message:
                text = message
                print(f"   message (as given):\n   > {text}")
            else:
                spoken = await write(model, first_name=first, tasks=tasks, now=now,
                                     order=settings.it_support_chat_providers,
                                     context=work_mod.describe(work, now))
                text = spoken.text
                print(f"   message ({spoken.who or 'plain fallback'}):\n   > {text}")
            if send_to:
                me = await graph.me()
                chat_id = await graph.chat_with(me["id"], person["id"])
                await graph.send(chat_id, talk.to_html(text))
                asked.mark(tasks, now)
                print("   SENT")


if __name__ == "__main__":
    import argparse
    import asyncio
    import sys

    sys.stdout.reconfigure(encoding="utf-8")  # the Windows console's cp1252 can't print "…"
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("sqlalchemy").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(description="Due check-ins: preview, or one real send.")
    parser.add_argument("--send", metavar="EMAIL", help="really send this one person's check-in")
    parser.add_argument("--day", choices=["today", "tomorrow"], default="today")
    parser.add_argument("--message", help="send exactly this, the wording already approved")
    args = parser.parse_args()
    if args.message and not args.send:
        parser.error("--message goes with --send")
    asyncio.run(_main(args.send, args.day, args.message))
