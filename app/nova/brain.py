"""Nova's answers: who is asking, what they may see, what the data says, and
the words.

**The data first, the model last.** For every message the app gathers, with
plain queries and no model, the tasks the message is about: tender or quote
numbers it names, people, dates ("today", "this week", "overdue"), and words
matching a task's title or customer — always inside what the person may see.
A fast model then writes the reply from those rows, the recent conversation
and a summary of the older one: the free ones first, Claude only when they
are busy (LLM_PROVIDERS). When no model answers in time, the rows are given as
they are, so a reply never waits on a model.

**What a person may see** is what they may read in reports
(``app.reports.access``): their own tasks; their teams' if they run a team;
everyone's if they run the company. Nova is shown nothing more, so it cannot
tell more.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Final
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.explain import relevant_glossary
from app.core.llm import TextModel
from app.mailwatch.matching import numbers_in, words
from app.models.nova import NovaMessage, NovaPerson
from app.models.proposal_index import ProposalIndexItem
from app.models.role import Role
from app.models.team import TeamMembership
from app.models.user import User
from app.reports.access import COMPANY_WIDE, TEAM_OVERSIGHT
from app.roles.service import global_role_keys

logger = logging.getLogger("hamdaz.nova")

DUBAI: Final = ZoneInfo("Asia/Dubai")

#: Rows Nova is shown at most for one message.
ROWS_SHOWN: Final = 12

#: Words of a chat that say nothing about which task: dropped before the
#: words left are matched against task titles.
_CHAT_WORDS: Final = frozenset(
    """what which when where whom whose who how why show tell give list about have has had
    does did doing done with from this that these those there their them they your yours
    mine please thanks thank hello hey hii today tomorrow yesterday week weeks month next
    last overdue late due open closed status task tasks tender tenders anything something
    everything any all team everyone anyone can could would should will shall need want
    know find check pending still also just more less many much some still update nova
    closing closes close deadline deadlines bcd""".split()
)

#: Thanks, greetings and the like: no task search behind them.
_SMALL_TALK: Final = re.compile(
    r"^\W*(hi|hii|hello|hey|thanks?|thank you|thx|ty|ok|okay|cool|great|nice|awesome|perfect"
    r"|good (morning|afternoon|evening|night)|bye|see you|you('re| are))\b",
    re.I,
)

_MENTION: Final = re.compile(r"<at>.*?</at>", re.S | re.I)
_TAG: Final = re.compile(r"<[^>]+>")
_ME: Final = re.compile(r"\b(my|mine|me|i)\b", re.I)


# ── who is asking ──────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Viewer:
    user_id: uuid.UUID
    email: str
    name: str
    roles: frozenset[str]
    #: Whose tasks they may see, by lower-case email; None for everyone's.
    emails: frozenset[str] | None

    @property
    def first_name(self) -> str:
        return (self.name or self.email).split()[0].split("@")[0].title()

    @property
    def sees_others(self) -> bool:
        return self.emails is None or len(self.emails) > 1

    def scope_words(self) -> str:
        if self.emails is None:
            return "every task in the company"
        if self.sees_others:
            return "their own tasks and those of the teams they run"
        return "their own tasks only"


async def viewer_of(session: AsyncSession, user: User) -> Viewer:
    roles = frozenset(await global_role_keys(session, user.id))
    if not COMPANY_WIDE.isdisjoint(roles):
        return Viewer(user.id, user.email, user.display_name, roles, None)
    runs = (
        select(TeamMembership.team_id)
        .join(Role, Role.id == TeamMembership.role_id)
        .where(TeamMembership.user_id == user.id, Role.key.in_(TEAM_OVERSIGHT))
    )
    members = await session.scalars(
        select(User.email)
        .join(TeamMembership, TeamMembership.user_id == User.id)
        .where(TeamMembership.team_id.in_(runs))
    )
    emails = frozenset({user.email.lower(), *(m.lower() for m in members.all() if m)})
    return Viewer(user.id, user.email, user.display_name, roles, emails)


# ── what the message is about ──────────────────────────────────────────


def clean(message: str) -> str:
    """The words typed: no @mention of Nova, no HTML Teams wraps them in."""
    plain = _TAG.sub(" ", _MENTION.sub(" ", message or ""))
    return re.sub(r"\s+", " ", plain.replace("&nbsp;", " ")).strip()


@dataclass(frozen=True, slots=True)
class Window:
    """A span of bid closing dates the message asks about."""

    start: date | None
    end: date
    label: str


def window_in(message: str, today: date) -> Window | None:
    m = message.lower()
    if re.search(r"\b(overdue|late|missed|past (?:the )?(?:due|deadline|bcd))\b", m):
        return Window(None, today - timedelta(days=1), "overdue (closing date passed, still open)")
    if re.search(r"\btoday\b", m):
        return Window(today, today, "closing today")
    if re.search(r"\btomorrow\b", m):
        return Window(today + timedelta(days=1), today + timedelta(days=1), "closing tomorrow")
    end_of_week = today + timedelta(days=6 - today.weekday())
    if re.search(r"\bnext week\b", m):
        return Window(end_of_week + timedelta(days=1), end_of_week + timedelta(days=7),
                      "closing next week")
    if re.search(r"\b(this week|week)\b", m):
        return Window(today, end_of_week, "closing this week")
    return None


def _days(when: date, today: date) -> str:
    gap = (when - today).days
    if gap == 0:
        return "today"
    if gap == 1:
        return "tomorrow"
    if gap == -1:
        return "yesterday"
    return f"in {gap} days" if gap > 0 else f"{-gap} days ago"


def line(row: ProposalIndexItem, today: date) -> str:
    """One task as Nova is shown it."""
    parts = [(row.title or "").strip() or "(untitled)"]
    if row.end_user:
        parts.append(row.end_user.strip())
    if row.assigned_name:
        parts.append(f"holder {row.assigned_name}")
    if row.bid_closing_date:
        parts.append(f"BCD {row.bid_closing_date:%d %b %Y} ({_days(row.bid_closing_date, today)})")
    status = row.effective_status or row.status
    if status:
        parts.append(f"status {status}")
    if row.submission_status:
        parts.append(f"submission {row.submission_status}")
    if row.quote_no:
        parts.append(f"quote {row.quote_no}")
    if row.order_status:
        parts.append(f"order {row.order_status}")
    if not row.is_open:
        parts.append("closed")
    return " · ".join(parts)


@dataclass
class Found:
    """What the data says about a message: rows, and why each group was
    picked, for the model and for a reply without one."""

    groups: list[tuple[str, list[ProposalIndexItem]]] = field(default_factory=list)
    #: What was looked for and not found ("closing this week"), so the reply
    #: can say there is none rather than talk about something else.
    none: list[str] = field(default_factory=list)
    #: Whether the message named a number, a person or a date — then a title
    #: search on its other words would only add noise.
    pointed: bool = False
    mine: list[ProposalIndexItem] = field(default_factory=list)
    open_count: int = 0

    def add(self, why: str, rows: list[ProposalIndexItem]) -> None:
        seen = {r.item_id for _, group in self.groups for r in group}
        fresh = [r for r in rows if r.item_id not in seen]
        if fresh:
            self.groups.append((why, fresh))
        elif not rows:
            self.none.append(why)

    @property
    def rows(self) -> list[ProposalIndexItem]:
        return [r for _, group in self.groups for r in group]


def _visible(query, viewer: Viewer, *, own: bool = False):
    query = query.where(ProposalIndexItem.deleted.is_(False))
    if own:
        return query.where(func.lower(ProposalIndexItem.assigned_email) == viewer.email.lower())
    if viewer.emails is not None:
        query = query.where(func.lower(ProposalIndexItem.assigned_email).in_(viewer.emails))
    return query


async def find(session: AsyncSession, viewer: Viewer, message: str, today: date) -> Found:
    found = Found()
    base = select(ProposalIndexItem)
    own = bool(_ME.search(message)) and viewer.sees_others

    numbers = sorted(numbers_in(message))
    if numbers:
        rows = await session.scalars(
            _visible(base, viewer)
            .where(or_(*[
                cond for n in numbers for cond in (
                    ProposalIndexItem.title.ilike(f"%{n}%"),
                    ProposalIndexItem.quote_no.ilike(f"%{n}%"),
                )
            ]))
            .order_by(ProposalIndexItem.sp_created_at.desc().nulls_last()).limit(6)
        )
        found.add(f"matching {', '.join(numbers)}", list(rows.all()))
        found.pointed = True

    if viewer.sees_others and not own:
        names = await session.scalars(
            _visible(select(ProposalIndexItem.assigned_name).distinct(), viewer).where(
                ProposalIndexItem.is_open.is_(True), ProposalIndexItem.assigned_name.is_not(None)
            )
        )
        said = set(re.findall(r"[a-z]+", message.lower()))
        for name in names.all():
            first = (name or "").split()[0].lower() if name else ""
            if len(first) > 2 and first in said:
                rows = await session.scalars(
                    _visible(base, viewer)
                    .where(ProposalIndexItem.is_open.is_(True),
                           ProposalIndexItem.assigned_name == name)
                    .order_by(ProposalIndexItem.bid_closing_date.asc().nulls_last()).limit(10)
                )
                found.add(f"{name}'s open tasks", list(rows.all()))
                found.pointed = True

    window = window_in(message, today)
    if window:
        query = _visible(base, viewer, own=own).where(
            ProposalIndexItem.is_open.is_(True),
            ProposalIndexItem.bid_closing_date <= window.end,
        )
        if window.start is not None:
            query = query.where(ProposalIndexItem.bid_closing_date >= window.start)
            order = ProposalIndexItem.bid_closing_date.asc()
        else:
            # Overdue: the ones missed most recently are the ones still worth chasing.
            order = ProposalIndexItem.bid_closing_date.desc()
        rows = await session.scalars(query.order_by(order).limit(15))
        found.add(window.label + (" (theirs only)" if own else ""), list(rows.all()))
        found.pointed = True

    if not found.pointed and not (_SMALL_TALK.match(message) and len(message.split()) <= 8):
        tokens = [w for w in dict.fromkeys(words(message)) if w not in _CHAT_WORDS]
        if tokens:
            query = func.to_tsquery(text("'english'::regconfig"), " | ".join(tokens))
            rows = await session.scalars(
                _visible(base, viewer)
                .where(ProposalIndexItem.search_vector.op("@@")(query))
                .order_by(ProposalIndexItem.is_open.desc(),
                          func.ts_rank(ProposalIndexItem.search_vector, query).desc())
                .limit(6)
            )
            found.add(f"titles or customers like '{' '.join(tokens)}'", list(rows.all()))

    # Their own open tasks, and how many — in one query: the upcoming ones
    # first, soonest closing, then those whose date has passed, latest first
    # (days from today, either way).
    bcd = ProposalIndexItem.bid_closing_date
    pairs = (await session.execute(
        _visible(select(ProposalIndexItem, func.count().over()), viewer, own=True)
        .where(ProposalIndexItem.is_open.is_(True))
        .order_by(case((bcd >= today, 0), else_=1), func.abs(bcd - today).asc().nulls_last())
        .limit(5)
    )).all()
    found.mine = [row for row, _ in pairs]
    found.open_count = int(pairs[0][1]) if pairs else 0
    return found


# ── memory ─────────────────────────────────────────────────────────────


async def history(session: AsyncSession, user_id: uuid.UUID, limit: int) -> list[NovaMessage]:
    """The person's last ``limit`` messages with Nova, oldest first."""
    rows = await session.scalars(
        select(NovaMessage).where(NovaMessage.user_id == user_id)
        .order_by(NovaMessage.created_at.desc()).limit(limit)
    )
    return list(reversed(rows.all()))


async def fold_old(
    session: AsyncSession, model: TextModel, person: NovaPerson, keep: int
) -> None:
    """Fold what has dropped out of view into the person's summary, once
    enough has: so the summary is rewritten every ``keep`` messages, not every
    message."""
    since = person.summarized_until or datetime.min.replace(tzinfo=UTC)
    rows = list((await session.scalars(
        select(NovaMessage)
        .where(NovaMessage.user_id == person.user_id, NovaMessage.created_at > since)
        .order_by(NovaMessage.created_at)
    )).all())
    if len(rows) < keep * 2:
        return
    old = rows[:-keep]
    answer = await model.ask(
        system=(
            "You keep a short memory of a chat between a colleague and Nova, a presales "
            "assistant. Merge the earlier memory with the messages into at most 8 short "
            "lines: what they asked about, tenders and customers they care about, their "
            "preferences, anything they asked Nova to remember. Facts only, no chit-chat. "
            'Reply with one JSON object: {"memory": "..."}'
        ),
        user=(
            f"Earlier memory:\n{person.summary or '(none)'}\n\nMessages:\n"
            + "\n".join(f"{m.author}: {m.text[:500]}" for m in old)
        ),
        max_tokens=500,
        check=lambda p: isinstance(p.get("memory"), str),
        label="nova memory",
        reasoning_effort="low",
    )
    if answer is None:
        return
    person.summary = answer.payload["memory"].strip()[:2000]
    person.summarized_until = old[-1].created_at


# ── the words ──────────────────────────────────────────────────────────

SYSTEM: Final = """\
You are Nova, the presales team's assistant at Hamdaz, chatting in Microsoft Teams.

Sound like a friendly, sharp colleague: short, warm and natural. Usually one to three
sentences; a short list only when listing tasks (one line each: number or title,
customer, closing date). Use their first name now and then, not every time. No
headings, no tables, at most one emoji and rarely.

Two kinds of facts. Hamdaz's own work (tasks, tender and quote numbers, dates,
statuses, who holds what, customers) comes only from DATA: never invent any of it, and
if DATA does not have it, say you can't see it and suggest what to ask. DATA is
exactly what this person may see; never hint at anything beyond it. Anything else
(what a term means, how something works, drafting an email, a translation, a general
question) answer from what you know, briefly and helpfully.
Bring up their tasks only when they ask about work or it clearly helps. To thanks,
greetings and small talk, just reply naturally in a sentence.
You can only read the ERP for now. If asked to change, assign, send or update anything, say
that is coming soon and point them to the Hamdaz app or SharePoint.
Dates are in Dubai time. Keep the conversation's thread: remember what was said before.

Reply with one JSON object and nothing else: {"reply": "<your message>"}"""


def prompt(
    viewer: Viewer, summary: str | None, past: list[NovaMessage], found: Found,
    message: str, now: datetime,
) -> str:
    today = now.date()
    data = [f"{why}:\n" + "\n".join(f"- {line(r, today)}" for r in rows[:ROWS_SHOWN])
            for why, rows in found.groups]
    if found.none:
        data.append("looked for, and there are none:\n"
                    + "\n".join(f"- {why}" for why in found.none))
    data.append(
        f"their own open tasks ({found.open_count} in all): upcoming first, then those whose "
        "closing date has passed (overdue, not due):\n"
        + ("\n".join(f"- {line(r, today)}" for r in found.mine) or "- none")
    )
    transcript = "\n".join(
        f"{viewer.first_name if m.author == 'person' else 'Nova'}: {m.text[:600]}" for m in past
    )
    return (
        f"Now: {now:%A %d %B %Y, %H:%M} (Dubai).\n"
        f"You are talking to {viewer.name} (first name {viewer.first_name}). "
        f"They may see {viewer.scope_words()}.\n\n"
        f"What you remember from earlier chats:\n{summary or '(nothing yet)'}\n\n"
        f"Recent messages:\n{transcript or '(this is the first message)'}\n"
        f"{relevant_glossary(message)}\n"
        "DATA (from the Proposals list):\n" + "\n\n".join(data) + "\n\n"
        f"{viewer.first_name} says: {message}"
    )


def plain_reply(viewer: Viewer, found: Found, today: date) -> str:
    """The reply when no model answered: what was found, as it is."""
    if found.groups:
        lines = [f"Here's what I found, {viewer.first_name}:"]
        for why, rows in found.groups:
            lines.append(f"**{why}**")
            lines += [f"- {line(r, today)}" for r in rows[:8]]
        return "\n".join(lines)
    if found.none:
        return f"Nothing {' or '.join(found.none)} that I can see, {viewer.first_name}."
    if found.mine:
        return "\n".join(
            [f"You have {found.open_count} open tasks; the soonest closing:"]
            + [f"- {line(r, today)}" for r in found.mine]
        )
    return (
        f"Sorry {viewer.first_name}, I couldn't work that one out just now. "
        "Try a tender number, 'my tasks', or 'what's due this week'."
    )


async def reply(
    model: TextModel, viewer: Viewer, summary: str | None, past: list[NovaMessage],
    found: Found, message: str, *, now: datetime, seconds: float,
) -> tuple[str, str]:
    """Nova's reply, and who wrote it: a model, or "rules" when none answered
    in ``seconds``."""
    try:
        answer = await asyncio.wait_for(
            model.ask(
                system=SYSTEM,
                user=prompt(viewer, summary, past, found, message, now),
                max_tokens=600,
                check=lambda p: isinstance(p.get("reply"), str) and bool(p["reply"].strip()),
                label="nova chat",
                reasoning_effort="low",
            ),
            timeout=seconds,
        )
    except TimeoutError:
        logger.info("nova: no model answered within %ss", seconds)
        answer = None
    if answer is None:
        return plain_reply(viewer, found, now.date()), "rules"
    return answer.payload["reply"].strip()[:4000], answer.who


def now_in_dubai() -> datetime:
    return datetime.now(DUBAI)
