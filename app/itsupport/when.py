"""Dates and times as IT Support says them, worked out here, never by the model.

The user's ask (2026-11-18): "make the agent get the realtime data, ie date
aware", before any other feature. Two things were missing:

* **The date.** The brain was told the clock ("9:05 AM UAE") but not the day,
  so "tomorrow", "this week" and "overdue since Monday" were guesses.
  :func:`today_line` gives the date, the weekday, both clocks and the working
  days.
* **Dates in what the ERP sends back.** Its JSON carries raw timestamps, and
  the Proposals list's BCD is stored as a US Pacific time that means the UAE
  (``followups.service.as_typed``): converted by hand, a model reads 10:30 AM
  as 10:30 PM. :func:`stamp` puts a ``<field>_said`` next to every date and
  time ("today 10:30 AM UAE (12:00 PM IST), in 1 h 25 min"), BCDs corrected
  first, and the model is told to use those words and never convert itself.

Times are always given on both clocks the team uses (UAE for BCDs, India for
part of the team), as ``work.both`` does.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Final
from zoneinfo import ZoneInfo

from app.core.config import Settings, get_settings
from app.followups import service as fu
from app.proposals.analytics import parse_when

UAE: Final = ZoneInfo("Asia/Dubai")
IST: Final = ZoneInfo("Asia/Kolkata")

#: List columns whose raw value is a BCD as the site's zone stored it: read as
#: the person typed it (``fu.as_typed``) before it is said.
RAW_BCD: Final = frozenset({"bid_closing_date", "placeholder_bcd", "BCD"})
#: Date-only list columns: SharePoint hands them back as the site's midnight.
DATE_COLUMNS: Final = frozenset({"due_date", "start_date", "DueDate", "StartDate"})
#: An ISO date or date-time, and nothing else ("2026-11-18", "…T06:30:00Z").
_ISO: Final = re.compile(
    r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?$"
)
_DAYS: Final = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def clock(moment: datetime, zone: ZoneInfo) -> str:
    return moment.astimezone(zone).strftime("%I:%M %p").lstrip("0")


def both(moment: datetime) -> str:
    """ "10:30 AM UAE (12:00 PM IST)"."""
    return f"{clock(moment, UAE)} UAE ({clock(moment, IST)} IST)"


def _day_word(day: date, today: date) -> str:
    if day == today:
        return "today"
    if day == today + timedelta(days=1):
        return "tomorrow"
    if day == today - timedelta(days=1):
        return "yesterday"
    return f"{day:%a %d %b}" + (f" {day.year}" if day.year != today.year else "")


def _span(minutes: int) -> str:
    hours, mins = divmod(minutes, 60)
    if not hours:
        return f"{mins} min"
    return f"{hours} h" + (f" {mins} min" if mins else "")


def from_now(moment: datetime, now: datetime) -> str:
    """ "in 1 h 25 min", "40 min ago", "in 3 days", "2 days ago", "now"."""
    seconds = (moment - now).total_seconds()
    if abs(seconds) < 60:
        return "now"
    if abs(seconds) < 24 * 3600:
        span = _span(int(abs(seconds) // 60))
    else:
        days = abs((moment.astimezone(UAE).date() - now.astimezone(UAE).date()).days)
        span = f"{days} day" + ("s" if days != 1 else "")
    return f"in {span}" if seconds > 0 else f"{span} ago"


def day_and_time(moment: datetime, now: datetime) -> str:
    """ "today 12:00 PM UAE (1:30 PM IST)", without how far off it is."""
    return f"{_day_word(moment.astimezone(UAE).date(), now.astimezone(UAE).date())} " \
           f"{both(moment)}"


def said(moment: datetime, now: datetime) -> str:
    """A moment as the team says it, with how far off it is:
    "today 10:30 AM UAE (12:00 PM IST), in 1 h 25 min"."""
    day = _day_word(moment.astimezone(UAE).date(), now.astimezone(UAE).date())
    return f"{day} {both(moment)}, {from_now(moment, now)}"


def said_day(day: date, now: datetime) -> str:
    """A date with no time: "today", "tomorrow", "Mon 30 Nov, in 12 days"."""
    today = now.astimezone(UAE).date()
    word = _day_word(day, today)
    if abs((day - today).days) <= 1:
        return word
    gap = (day - today).days
    return f"{word}, " + (f"in {gap} days" if gap > 0 else f"{-gap} days ago")


def _said_value(key: str, value: str, now: datetime) -> str | None:
    text = value.strip()
    if not _ISO.match(text):
        return None
    if len(text) == 10:
        try:
            return said_day(date.fromisoformat(text), now)
        except ValueError:
            return None
    moment = parse_when(text)
    if moment is None:
        return None
    if key in RAW_BCD:
        return said(fu.as_typed(moment), now)
    if key in DATE_COLUMNS:
        # Mostly the site's midnight (T07:00Z / T08:00Z); some rows, written
        # by other tools, UTC midnight (T00:00Z). Read 2026-11-18 on the list.
        site = moment.astimezone(ZoneInfo(get_settings().sharepoint_site_timezone))
        if site.time() == time(0, 0):
            return said_day(site.date(), now)
        if moment.astimezone(UTC).time() == time(0, 0):
            return said_day(moment.astimezone(UTC).date(), now)
    return said(moment, now)


def stamp(body: Any, now: datetime) -> Any:
    """The ERP's JSON with a ``<field>_said`` next to every date and time.
    The raw values stay as they were (a change may need them)."""
    if isinstance(body, list):
        return [stamp(item, now) for item in body]
    if not isinstance(body, dict):
        return body
    out: dict[str, Any] = {}
    for key, value in body.items():
        out[key] = stamp(value, now)
        if isinstance(value, str) and f"{key}_said" not in body and (
                words := _said_value(str(key), value, now)):
            out[f"{key}_said"] = words
    return out


# ── today ──────────────────────────────────────────────────────────────


def _office(settings: Settings) -> tuple[ZoneInfo, set[int], time, time]:
    from app.itsupport.due import _office as office  # due imports work; work imports this

    return office(settings)


def _days_said(days: set[int]) -> str:
    ordered = sorted(d for d in days if 0 <= d <= 6)
    if not ordered:
        return "no days"
    if ordered == list(range(ordered[0], ordered[-1] + 1)) and len(ordered) > 2:
        return f"{_DAYS[ordered[0]]} to {_DAYS[ordered[-1]]}"
    return ", ".join(_DAYS[d] for d in ordered)


def today_line(now: datetime, settings: Settings) -> str:
    """Today's date, weekday and time on both clocks, and the working days:
    what the model needs to say "tomorrow" or "overdue" right."""
    zone, days, start, end = _office(settings)
    local = now.astimezone(UAE)
    today = now.astimezone(zone).date()
    working = today.weekday() in days
    out = (f"Today is {local:%A %d %B %Y} ({'a working day' if working else 'a day off'}); "
           f"it's {both(now)}.")
    tomorrow = today + timedelta(days=1)
    if tomorrow.weekday() in days:
        out += f" Tomorrow is {tomorrow:%A %d %b}."
    else:
        nxt = tomorrow
        for _ in range(7):
            if nxt.weekday() in days:
                break
            nxt += timedelta(days=1)
        out += (f" Tomorrow is {tomorrow:%A %d %b}, a day off; "
                f"the next working day is {nxt:%A %d %b}.")
    opens = datetime.combine(today, start, tzinfo=zone)
    closes = datetime.combine(today, end, tzinfo=zone)
    out += (f" Office hours: {_days_said(days)}, {clock(opens, zone)} to {clock(closes, zone)} "
            f"{'IST' if zone.key == 'Asia/Kolkata' else zone.key}.")
    return out
