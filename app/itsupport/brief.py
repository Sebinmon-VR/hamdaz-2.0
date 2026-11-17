"""The morning brief: IT Support's first message of the day to each person.

The user kept this idea (2026-11-13, team feature 1) and asked for it next
(2026-11-17): at the start of the working day (``it_support_brief_at``,
09:30 IST), each person it answers gets one short message, the way a
colleague would say it: what closes today, what closes early on the next
working day (before they'd be properly in), and what is still open past its
BCD. Nothing due, nothing sent: a colleague doesn't message to say "nothing
today".

Rules first: which tasks, who is away (leave, notice period, switched off;
``mailwatch.availability.away_reason``), once a day per person, all decided
here without a model. The model only puts the facts into words, in the
chat's own voice, with a plain fallback.

The brief and the due-today check-ins (``due``) don't repeat each other:
today's tasks named in the brief are marked as asked about, so no "need a
hand?" follows for them, and check-ins wait until the brief time has passed.

Reads the Proposals list; never writes it. Off unless the settings page (or
IT_SUPPORT_BRIEF_ENABLED) turns it on.

Preview, sending nothing; one real send only when told to::

    uv run python -m app.itsupport.brief
    uv run python -m app.itsupport.brief --send sebin@hamdaz.com
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Final

from app.core.config import Settings
from app.core.llm import Spoken, TextModel
from app.followups import service as fu
from app.itsupport import due, talk
from app.itsupport.due import DueTask, short
from app.itsupport.work import UAE, Work, both
from app.reminders.service import is_done

logger = logging.getLogger("hamdaz.itsupport")

#: A brief later than this after its time is not a morning brief any more
#: (the laptop came on at 3 PM): it is skipped for the day.
LATE_BY: Final = timedelta(hours=2)
#: Past its BCD and still open, for this long, it is mentioned as late.
LATE_FOR: Final = timedelta(days=3)


@dataclass(slots=True)
class Brief:
    today: list[DueTask] = field(default_factory=list)
    #: Early on the next working day: before office opens + ``due.EARLY``.
    next_day: list[DueTask] = field(default_factory=list)
    #: BCD passed in the last ``LATE_FOR``, not submitted or completed.
    late: list[DueTask] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.today or self.next_day or self.late)

    def lines(self) -> str:
        out = [f"- {t.title} (closes today {t.at})" for t in self.today]
        out += [f"- {t.title} (closes {t.when} {t.at}, early)" for t in self.next_day]
        out += [f"- {t.title} (BCD was {t.at}, {t.when}; still open)" for t in self.late]
        return "\n".join(out)


def plan(work: Work, *, now: datetime, settings: Settings) -> Brief:
    """What the brief says, from the person's tasks; rules only."""
    today_uae = now.astimezone(UAE).date()
    asked = due.to_ask(work, now=now, settings=settings)
    brief = Brief(
        today=[t for t in asked if t.due_at.astimezone(UAE).date() == today_uae],
        next_day=[t for t in asked if t.due_at.astimezone(UAE).date() != today_uae],
    )
    for task in work.tasks:
        moment = fu.due_of(task)
        if moment is None or is_done(task) or task.id in work.held:
            continue
        if now - LATE_FOR <= moment < now:
            day = moment.astimezone(UAE).date()
            when = "today" if day == today_uae else (
                "yesterday" if day == today_uae - timedelta(days=1) else f"on {day:%A}")
            brief.late.append(DueTask(task.id, (task.title or "").strip(), moment,
                                      task.status, when))
    brief.late.sort(key=lambda t: t.due_at)
    return brief


def due_now(settings: Settings, now: datetime) -> date | None:
    """The office day whose brief is due now (its time passed, not by more
    than ``LATE_BY``, on a working day), else None."""
    zone, days, _, _ = due._office(settings)
    local = now.astimezone(zone)
    at = due._clock(settings.it_support_brief_at, "09:30")
    start = local.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
    if local.weekday() in days and start <= local < start + LATE_BY:
        return local.date()
    return None


def before_brief(settings: Settings, now: datetime) -> bool:
    """Whether today's brief is still to come (check-ins wait for it)."""
    if not settings.it_support_brief_enabled:
        return False
    zone, _, _, _ = due._office(settings)
    local = now.astimezone(zone)
    at = due._clock(settings.it_support_brief_at, "09:30")
    return local.time().replace(tzinfo=None) < at


def sent_key(email: str, day: date) -> str:
    """The once-a-day key in ``due.Asked``'s file ("…@date" is what it prunes by)."""
    return f"brief:{email.lower()}@{day.isoformat()}"


# ── the message ────────────────────────────────────────────────────────

ASK: Final = """\
(Not a message from them: you're starting the day's chat yourself. They haven't written.)
It's the start of their working day. From their tasks (bid closing times):

{lines}

Write the one message you'd send a colleague first thing: a quick good morning, then what's
on today in a line or two (times exactly as given, both UAE and IST). If something is past
its BCD and still open, mention it plainly, without blame. End with a short offer to help
only if it fits. Two or three short sentences, casual, like a teammate, not a report: no
headings, no list unless there are four or more tasks. Call a task by its RFQ or tender
number plus a few words of what it is, never the whole title. Don't mention statuses, the
Proposals list, or that you were told to write this. Only the message itself."""


def fallback(first_name: str, brief: Brief) -> str:
    parts = []
    if brief.today:
        parts.append("today: " + ", ".join(f"{short(t.title)} at {t.at}" for t in brief.today))
    if brief.next_day:
        parts.append(", ".join(f"{short(t.title)} {t.when} at {t.at}" for t in brief.next_day)
                     + " (early, so worth a look today)")
    if brief.late:
        parts.append("still open past its BCD: " + ", ".join(short(t.title) for t in brief.late))
    return f"Morning {first_name}! On your plate {'; '.join(parts)}. Shout if you need a hand."


async def write(model: TextModel, *, first_name: str, brief: Brief, now: datetime,
                order: str = "", context: str = "") -> Spoken:
    spoken = Spoken()
    try:
        async for _ in model.chat(
            system=talk.system_for(first_name, now, context=context),
            messages=[{"role": "user", "content": ASK.format(lines=brief.lines())}],
            spoken=spoken, order=order, max_tokens=300, label="it support morning brief",
        ):
            pass
    except Exception as exc:  # noqa: BLE001 - the plain message still goes
        logger.warning("it support: the brief's model failed: %s", exc)
        spoken = Spoken()
    text = spoken.text.strip().strip('"').strip()
    spoken.text = text or fallback(first_name, brief)
    return spoken


# ── preview, and the one-off send the user asks for ────────────────────


async def _main(send_to: str | None) -> None:
    import httpx

    from app.core.config import get_settings
    from app.core.db import get_session_factory, init_engine
    from app.itsupport import settings as settings_mod
    from app.itsupport import work as work_mod
    from app.itsupport.graph import ITSupportGraph
    from app.mailwatch.availability import away_reason
    from app.proposals.sharepoint import SharePointProposals

    settings = get_settings().model_copy()
    init_engine(settings)
    logging.getLogger("sqlalchemy.engine.Engine").setLevel(logging.WARNING)  # DB echo
    factory = get_session_factory()
    if (row := await settings_mod.load(factory, settings)) is not None:
        settings_mod.apply(row, settings)
    now = datetime.now(UTC)
    print(f"now: {both(now)} · brief at {settings.it_support_brief_at} "
          f"({settings.it_support_due_zone}) · "
          f"{'SENDING to ' + send_to if send_to else 'preview, nothing is sent'}")
    async with httpx.AsyncClient(timeout=30) as http:
        sharepoint = SharePointProposals(settings, http)
        model = TextModel(settings, http)
        graph = ITSupportGraph(settings, http, get_session_factory())
        people = [send_to] if send_to else [
            e.strip() for e in settings.it_support_allowed_emails.split(",")
            if e.strip() not in ("", "*")]
        for email in people:
            async with factory() as session:
                away = await away_reason(session, email)
            if away:
                print(f"\n-- {email}: away ({away}), no brief")
                continue
            work = await work_mod.read(sharepoint, factory, email)
            brief = plan(work, now=now, settings=settings)
            if brief.empty:
                print(f"\n-- {email}: nothing due, no brief")
                continue
            print(f"\n-- {email}:\n{brief.lines()}")
            person = await graph.user(email)
            first = talk.first_name(settings, email, person.get("displayName"))
            spoken = await write(model, first_name=first, brief=brief, now=now,
                                 order=settings.it_support_chat_providers,
                                 context=work_mod.describe(work, now))
            print(f"   message ({spoken.who or 'plain fallback'}):\n   > {spoken.text}")
            if send_to:
                me = await graph.me()
                chat_id = await graph.chat_with(me["id"], person["id"])
                await graph.send(chat_id, talk.to_html(spoken.text))
                print("   SENT")


if __name__ == "__main__":
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(description="Preview the morning brief.")
    parser.add_argument("--send", metavar="EMAIL",
                        help="send this one person's brief for real (only when told to)")
    args = parser.parse_args()
    asyncio.run(_main(args.send))
