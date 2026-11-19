"""A team's tasks as IT Support lists them in Teams: the morning brief, and
every list asked for after it ("only not submitted", "Haleema's", "the late
ones", "what's closing tomorrow").

The user, 2026-11-19, on the brief: "the format is not good"; and on "show
only not submitted" getting "I couldn't filter this properly" (Haiku had read
the raw Proposals feed for 145k tokens and given up). So lists are rules, end
to end: the rows come from the team's list as read a minute ago, a filter is
applied by code, and each section goes out as a small table, which Teams
shows as one. A list keeps the numbers of the one before it, so "check with
3" means the same task in both. The model never types rows: its
``team_tasks`` tool puts ``[[list]]`` in its reply and the table goes there.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Final

from app.followups import service as fu
from app.itsupport import when
from app.itsupport.status import Asked, Item
from app.proposals.sharepoint import ProposalTask
from app.reminders.service import is_done

#: The sections of a list.
TODAY: Final = "today"
LATE: Final = "late"
UNSET: Final = "unset"
TOMORROW: Final = "tomorrow"
WEEK: Final = "week"

#: A BCD passed this many days ago, still open, is "late"; older is the
#: overdue follow-up's business.
LATE_DAYS: Final = 3
#: A task still on its assignment-time BCD is listed this long after it came.
UNSET_DAYS: Final = 3
#: How far "this week" looks ahead.
WEEK_DAYS: Final = 7

#: Where the model's reply takes the table its team_tasks tool made.
MARK: Final = "[[list]]"


@dataclass(slots=True)
class Member:
    """One teammate, as the list shows them."""

    #: What the team calls them ("Vishnu", not Teams' "Sri Vishnu").
    name: str
    #: Why they're away today, or None.
    away: str | None
    tasks: list[ProposalTask]
    email: str = ""
    #: Their name in the app, every word of which finds them ("sri vishnu").
    full_name: str = ""


@dataclass(slots=True)
class Row:
    task: ProposalTask
    who: str
    away: str | None
    section: str
    #: The BCD; for a task still on its placeholder, when it was assigned.
    at: datetime | None
    done: bool
    number: int = 0


@dataclass(slots=True)
class Shown:
    """A list ready to send: the message, its first line in words (for the
    log), and the numbered tasks in it (for "check with 2")."""

    html: str
    line: str
    items: list[Item] = field(default_factory=list)
    count: int = 0


# ── rows ───────────────────────────────────────────────────────────────


def collect(members: list[Member], held: set[str], now: datetime, *,
            ahead: int = 0) -> list[Row]:
    """Today's tasks (done ones too), the late ones, the ones still on a
    placeholder BCD, and, with ``ahead``, the open ones of the next days."""
    today = now.astimezone(when.UAE).date()
    out: list[Row] = []
    for member in members:
        for task in member.tasks:
            done = is_done(task)
            if task.id in held:
                # Its BCD is still the time it was assigned: not due then.
                created = when.parse_when(task.created_at)
                if not done and created is not None and \
                        now - created <= timedelta(days=UNSET_DAYS):
                    out.append(Row(task, member.name, member.away, UNSET, created, done))
                continue
            due = fu.due_of(task)
            if due is None:
                continue
            day = due.astimezone(when.UAE).date()
            if day == today:
                out.append(Row(task, member.name, member.away, TODAY, due, done))
            elif day < today:
                if not done and (today - day).days <= LATE_DAYS:
                    out.append(Row(task, member.name, member.away, LATE, due, done))
            elif not done and (day - today).days <= ahead:
                section = TOMORROW if day == today + timedelta(days=1) else WEEK
                out.append(Row(task, member.name, member.away, section, due, done))
    # The order they're shown in, so the numbers run down the message.
    order = {TODAY: 0, TOMORROW: 1, WEEK: 2, LATE: 3, UNSET: 4}
    return sorted(out, key=lambda r: (order[r.section], r.at or now))


def number(rows: list[Row], earlier: Asked | None) -> list[Item]:
    """Each open row its number: the one it had in the last list, else the
    next free one. Done rows aren't numbered (nothing to chase)."""
    kept = {i.task_id: i.number for i in earlier.items} if earlier is not None and \
        earlier.kind == "team" else {}
    items = {i.task_id: i for i in earlier.items} if kept and earlier is not None else {}
    free = max(kept.values(), default=0) + 1
    for row in rows:
        if row.done:
            continue
        if row.task.id in kept:
            row.number = kept[row.task.id]
        else:
            row.number = free
            free += 1
            items[row.task.id] = Item(row.number, row.task.id, row.task.title or "")
    return sorted(items.values(), key=lambda i: i.number)


# ── what to show: read from their words ────────────────────────────────


@dataclass(frozen=True, slots=True)
class Filter:
    #: "open" (not submitted, not completed), "done", or "" for both.
    state: str = ""
    #: The teammates meant, as the list names them.
    who: frozenset[str] = frozenset()
    #: The sections meant; empty: those of the brief (today, late, unset).
    sections: frozenset[str] = frozenset()

    @property
    def ahead(self) -> int:
        if WEEK in self.sections:
            return WEEK_DAYS
        return 1 if TOMORROW in self.sections else 0

    def apply(self, rows: list[Row]) -> list[Row]:
        wanted = self.sections or frozenset({TODAY, LATE, UNSET})
        if WEEK in wanted:
            wanted = wanted | {TODAY, TOMORROW}
        out = [r for r in rows if r.section in wanted]
        if self.state == "open":
            out = [r for r in out if not r.done]
        elif self.state == "done":
            out = [r for r in out if r.done]
        if self.who:
            out = [r for r in out if r.who in self.who]
        return out

    def said(self) -> str:
        """ "Haleema's, not submitted yet", "Late, still open"."""
        bits = []
        if self.who:
            bits.append(" and ".join(sorted(self.who)) + ("'s" if len(self.who) == 1 else ""))
        names = {TODAY: "closing today", LATE: "past BCD", UNSET: "BCD not set yet",
                 TOMORROW: "closing tomorrow", WEEK: "this week"}
        bits += [names[s] for s in (TODAY, TOMORROW, WEEK, LATE, UNSET) if s in self.sections]
        if self.state == "open":
            bits.append("not submitted yet")
        elif self.state == "done":
            bits.append("submitted or completed")
        text = ", ".join(bits) or "the team's tasks"
        return text[0].upper() + text[1:]


_OPEN: Final = re.compile(
    r"\b(?:not|isn'?t|aren'?t|hasn'?t|haven'?t|yet\s+to|without|remove|hide|exclude|except|"
    r"drop|skip|minus)\s+(?:been\s+|be\s+|yet\s+|the\s+)?(?:submit\w*|done|complete\w*|finish\w*)"
    r"|\bun-?submitted\b|\b(?:pending|open|remaining|outstanding|left|not\s+done)\b", re.I)
_DONE: Final = re.compile(r"\b(?:submit\w*|done|complete\w*|finish\w*)\b", re.I)
_SECTIONS: Final = (
    (UNSET, re.compile(r"\b(?:bcd\s+)?not\s+set\b|\bunset\b|\bno\s+bcd\b|\bnew\s+ones\b", re.I)),
    (LATE, re.compile(r"\b(?:late|overdue|past|missed|passed|delayed)\b", re.I)),
    (TOMORROW, re.compile(r"\btomorrow'?s?\b|\btmrw\b|\btmr\b", re.I)),
    (WEEK, re.compile(r"\b(?:this\s+)?week'?s?\b|\bupcoming\b|\bcoming\s+up\b", re.I)),
    (TODAY, re.compile(r"\btoday'?s?\b|\btdy\b", re.I)),
)
#: Words a list-asking message is made of, besides the filter's own words and
#: the teammates' names. One word outside these and it isn't only a filter:
#: the model takes it ("why is 1958 late", "remind haleema").
_FILLER: Final = frozenset([
    "show", "only", "just", "list", "give", "me", "the", "ones", "one", "those", "these",
    "that", "which", "what", "are", "is", "of", "for", "by", "with", "all", "pls", "plz",
    "please", "can", "could", "you", "u", "now", "again", "them", "items", "tasks", "task",
    "bids", "bid", "and", "or", "yet", "still", "who", "whose", "not", "to", "from", "in",
    "team", "presale", "s", "bcd", "set", "no", "new", "any", "there", "filter", "see",
    "display", "tell", "send", "let", "know", "their", "his", "her", "it", "its", "ok", "okay",
    "so", "then", "also", "want", "need", "show's", "whats", "what's", "lists", "sorted", "due",
    "closing", "close", "closes", "deadline", "deadlines", "bcds", "everyone", "everybody",
    "each", "person", "people", "member", "members"])
_STATE_WORDS: Final = frozenset([
    "not", "isnt", "isn't", "arent", "aren't", "hasnt", "hasn't", "havent", "haven't", "yet",
    "without", "remove", "hide", "exclude", "except", "drop", "skip", "minus", "been", "be",
    "submit", "submitted", "unsubmitted", "un", "done", "complete", "completed", "finish",
    "finished", "pending", "open", "remaining", "outstanding", "left"])
_SECTION_WORDS: Final = frozenset([
    "late", "overdue", "past", "missed", "passed", "delayed", "tomorrow", "tomorrows", "tmrw",
    "tmr", "week", "weeks", "this", "upcoming", "coming", "up", "today", "todays", "tdy",
    "unset"])


def read_filter(words: str, names: dict[str, str]) -> Filter | None:
    """A message that only narrows the team's list, read by rules; None for
    anything else. ``names``: every word of a teammate's names, lower case →
    the name the list shows ("sri" → "Vishnu", "vishnu" → "Vishnu")."""
    text = words.strip()
    if not text or "?" in text and re.search(r"\b(why|when|how)\b", text, re.I):
        return None
    tokens = [t.removesuffix("'s").removesuffix("’s")
              for t in re.findall(r"[a-z’']+", text.casefold())]
    if not tokens or re.search(r"\d", text):
        return None
    who: set[str] = set()
    for token in tokens:
        if token in names:
            who.add(names[token])
        elif token.endswith("s") and token[:-1] in names:
            who.add(names[token[:-1]])  # "haleemas"
        elif token not in _FILLER and token not in _STATE_WORDS and \
                token not in _SECTION_WORDS:
            return None
    state = "open" if _OPEN.search(text) else "done" if _DONE.search(text) else ""
    sections: set[str] = set()
    rest = text
    for section, pattern in _SECTIONS:
        if pattern.search(rest):
            sections.add(section)
            rest = pattern.sub(" ", rest)
    if not state and not who and not sections:
        return None
    return Filter(state, frozenset(who), frozenset(sections))


# ── how it reads in Teams ──────────────────────────────────────────────


_LEAD: Final = re.compile(r"^(\d{6,})\b[\s\-–—:]*")
_KIND: Final = r"(?:(?:single|two)\s+stage\s+)?(?:rfq|rft|rfp|rfi|tender)"


def tender(title: str, limit: int = 46) -> str:
    """A task's title as the list shows it: the tender number once, then what
    it's for. "6000155983 RFQ 6000155983 TOOLS, DRILL BITS" → "6000155983 ·
    TOOLS, DRILL BITS"; "6000154934 Single Stage RFQ 6000154934" → the number."""
    text = " ".join((title or "").replace("_", " ").split())
    found = _LEAD.match(text)
    if found is None:
        return _cut(text, limit + 13)
    ref, rest = found.group(1), text[found.end():]
    rest = re.sub(rf"(?:\b{_KIND}\s*[-#:]?\s*)?{ref}\b[\s\-–—:]*", " ", rest, flags=re.I)
    rest = re.sub(rf"^\s*{_KIND}\s*$", "", rest, flags=re.I)
    # A code said twice ("PR 4001754766 - PR 4001754766") once.
    seen: set[str] = set()
    out: list[str] = []
    for word in rest.split():
        key = word.casefold().strip("-:,.")
        if any(c.isdigit() for c in key) and key in seen:
            if out and out[-1].isalpha() and out[-1].isupper() and len(out[-1]) <= 3:
                out.pop()
            continue
        seen.add(key)
        out.append(word)
    rest = re.sub(r"(?:\s*[-–—·:,]\s*){2,}", " - ", " ".join(out)).strip(" -–—·:,")
    return ref + (f" · {_cut(rest, limit)}" if rest else "")


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip(" -,·(") + "…"


def _day(moment: datetime, today: date) -> str:
    day = moment.astimezone(when.UAE).date()
    if day == today:
        return ""
    if day == today - timedelta(days=1):
        return "Yesterday "
    if day == today + timedelta(days=1):
        return "Tomorrow "
    # Within the week a weekday says it; a date only beyond (a long cell
    # wrapped the row, 2026-11-19).
    if abs((day - today).days) <= 6:
        return f"{day:%a} "
    return f"{day:%d %b} "


def when_cells(at: datetime, now: datetime) -> tuple[str, str]:
    """A moment's two cells: "Mon 3:45 PM" (UAE, the day only when not
    today) and "5:15 PM" (IST)."""
    today = now.astimezone(when.UAE).date()
    return f"{_day(at, today)}{when.clock(at, when.UAE)}", when.clock(at, when.IST)


def _state(row: Row) -> str:
    task = row.task
    if row.done:
        said = task.submission_status if fu.is_submitted(task.submission_status) \
            else task.status
        return f"✓ {said or 'Done'}"
    return task.status or "Not set"


_HEADS: Final = {TODAY: "BCD (UAE)", LATE: "Closed (UAE)", UNSET: "Assigned (UAE)",
                 TOMORROW: "BCD (UAE)", WEEK: "BCD (UAE)"}
_TITLES: Final = {TODAY: "Closing today", LATE: "Past BCD, still open",
                  UNSET: "BCD not set yet", TOMORROW: "Closing tomorrow",
                  WEEK: "Later this week"}


#: A blank line between the parts of a list. Teams sets paragraphs tight, and
#: the sections ran into each other (the user, 2026-11-19: "add some space").
SPACE: Final = "<p>&nbsp;</p>"
#: A cell's tender at most this long, so a row stays on one line.
TENDER_CHARS: Final = 38


def grid(head: list[str], rows: list[list[str]]) -> str:
    """A Teams table. Plain headings: bold ones on every table made the
    message heavy (2026-11-19: "the dark text are making mess")."""
    lines = ["<tr>" + "".join(f"<td>{html.escape(c)}</td>" for c in cells) + "</tr>"
             for cells in [head, *rows]]
    return "<table><tbody>" + "".join(lines) + "</tbody></table>"


def table(rows: list[Row], section: str, now: datetime) -> str:
    """One section as a Teams table: #, Task, Tender, Who, the time in both
    clocks, Status."""
    cells = []
    for row in rows:
        uae, ist = when_cells(row.at, now) if row.at else ("", "")
        if section == TODAY and row.at is not None and row.at < now and not row.done:
            uae += " (passed)"
        cells.append([str(row.number or ""), row.task.id,
                      tender(row.task.title or "", TENDER_CHARS),
                      row.who + (" (away)" if row.away else ""), uae, ist, _state(row)])
    return grid(["#", "Task", "Tender", "Who", _HEADS[section], "IST", "Status"], cells)


def heading(name: str, rest: str = "") -> str:
    """A section's line after a blank line, only its name bold."""
    return f"{SPACE}<p><b>{html.escape(name)}</b>{html.escape(rest)}</p>"


def _done_line(rows: list[Row]) -> str:
    said = ", ".join(f"{r.task.id} ({r.who}, {_state(r).removeprefix('✓ ').lower()})"
                     for r in rows)
    return f"<p>Already in today: {html.escape(said)}</p>"


def sections_html(rows: list[Row], now: datetime, *, done_inline: bool) -> str:
    """Every section that has rows: a bold line and its table. Today's done
    tasks as one line (``done_inline``) or in today's table."""
    parts: list[str] = []
    for section in (TODAY, TOMORROW, WEEK, LATE, UNSET):
        found = [r for r in rows if r.section == section]
        if not found:
            continue
        done = [r for r in found if r.done]
        shown = [r for r in found if not r.done] if done_inline else found
        if shown:
            count = f"{len(shown)} open" if section == TODAY and done_inline else str(len(shown))
            rest = f" · {count}"
            if section == UNSET:
                rest += " (it still shows when it was assigned)"
            parts.append(heading(_TITLES[section], rest) + table(shown, section, now))
        if done_inline and done:
            parts.append(_done_line(done))
    return "".join(parts)


def brief(first_name: str, teams: list[tuple[str, list[Row]]], now: datetime,
          earlier: Asked | None = None) -> Shown | None:
    """The morning brief of the teams they oversee; None when nothing's due."""
    teams = [(name, rows) for name, rows in teams if rows]
    if not teams:
        return None
    items = number([r for _, rows in teams for r in rows], earlier)
    out, line = [], ""
    for name, rows in teams:
        today = [r for r in rows if r.section == TODAY]
        open_today = sum(not r.done for r in today)
        summary = (f"{name}: {len(today)} closing today, {len(today) - open_today} already in"
                   if today else f"{name}: nothing closing today")
        line = line or summary
        if len(teams) > 1:
            team, _, said = summary.partition(":")
            out.append(heading(team, ":" + said))
        out.append(sections_html(rows, now, done_inline=True))
    opening = f"Morning {first_name}. " + (line + "." if len(teams) == 1 else "")
    closing = "Want me to check with anyone? Just say the number." if items else ""
    body = f"<p>{html.escape(opening.strip())}</p>" + "".join(out) + \
        (f"{SPACE}<p>{html.escape(closing)}</p>" if closing else "")
    return Shown(body, opening.strip(), items, sum(not r.done for _, rs in teams for r in rs))


def filtered(teams: list[tuple[str, list[Row]]], flt: Filter, now: datetime,
             earlier: Asked | None) -> Shown:
    """A list after the brief, narrowed as they asked, the brief's numbers kept."""
    teams = [(name, flt.apply(rows)) for name, rows in teams]
    found = [r for _, rows in teams for r in rows]
    items = number(found, earlier)
    label = flt.said()
    if not found:
        line = f"{label}: none right now."
        if flt.state == "open" and not flt.who:
            line = "Nothing open: everything on that list is submitted or completed."
        return Shown(f"<p>{html.escape(line)}</p>", line, items, 0)
    line = f"{label} ({len(found)}):"
    parts = [f"<p>{html.escape(line)}</p>"]
    for name, rows in teams:
        if not rows:
            continue
        if len([t for t in teams if t[1]]) > 1:
            parts.append(heading(name))
        parts.append(sections_html(rows, now, done_inline=False))
    return Shown("".join(parts), line, items, len(found))


def describe_rows(rows: list[Row], now: datetime) -> str:
    """The rows in words, for the model (it reads; the table is what's sent)."""
    out = []
    for r in rows:
        at = when.day_and_time(r.at, now) if r.at else "no BCD"
        out.append(f"{r.number or '-'}. {r.task.id} {tender(r.task.title or '', 60)} | "
                   f"{r.who}{' (away)' if r.away else ''} | {_TITLES[r.section]} | {at} | "
                   f"{_state(r)}")
    return "\n".join(out)


def fill(body: str, block: str | None) -> str:
    """The model's reply with its table in place of ``[[list]]``."""
    if not block:
        return body.replace(MARK, "")
    pattern = re.compile(r"(?:<br>)*" + re.escape(MARK) + r"(?:<br>)*")
    if pattern.search(body):
        return pattern.sub(lambda _: block, body, count=1).replace(MARK, "")
    return body + block
