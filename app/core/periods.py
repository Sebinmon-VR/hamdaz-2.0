"""What a period is, in one place.

Two modules needed to answer "which days does *this week* cover" — the reports
module, which has always had daily, weekly and monthly reports, and the projects
module, which needs the same answer plus quarters and years so a status report
can cover any of them. Both had a version of this arithmetic and they agreed
right up until somebody added a grain to one of them.

So the calendar lives here and nothing else does. The two modules keep their own
vocabularies — reports call it a *cadence*, projects a *grain* — because those
words mean different things to their users, and both map onto the grains below.

Every window is **inclusive at both ends**, so a daily window's two dates are
the same day. That keeps every range query in the app written one way instead
of two, and it is the convention the reports module already used.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Final, Literal

#: The calendar grains. ``custom`` is the escape hatch: the caller names both
#: ends, which is what a report on a phase or a sprint needs and what no
#: calendar grain can express.
Grain = Literal["day", "week", "month", "quarter", "year", "custom"]

GRAINS: Final[tuple[str, ...]] = ("day", "week", "month", "quarter", "year", "custom")


def window_for(grain: str, on: date) -> tuple[date, date]:
    """The period a grain covers, from any day inside it.

    Weeks run Monday to Sunday — the ISO week, so "week 41" means here what it
    means in every calendar the company already has open. Quarters are calendar
    quarters starting in January, not a financial year: nothing in this system
    knows when the financial year starts, and guessing would be worse than
    being plainly calendar-based.

    ``custom`` returns the single day and expects the caller to replace both
    ends, because nothing else can know what they meant.
    """
    if grain == "week":
        start = on - timedelta(days=on.weekday())
        return start, start + timedelta(days=6)

    if grain == "month":
        start = on.replace(day=1)
        # Add enough days to land in the next month whatever this one's length,
        # then take the first of it. Avoids a month-length table and the
        # February special case that always comes with one.
        return start, (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)

    if grain == "quarter":
        first_month = 3 * ((on.month - 1) // 3) + 1
        start = on.replace(month=first_month, day=1)
        after = (
            date(start.year + 1, 1, 1)
            if first_month == 10
            else date(start.year, first_month + 3, 1)
        )
        return start, after - timedelta(days=1)

    if grain == "year":
        return on.replace(month=1, day=1), on.replace(month=12, day=31)

    return on, on


def window_label(grain: str, start: date, end: date) -> str:
    """How a period reads to a person. Used in titles, headings and email."""
    if grain == "day":
        return start.strftime("%A %d %B %Y")
    if grain == "week":
        return f"week of {start.strftime('%d %B %Y')}"
    if grain == "month":
        return start.strftime("%B %Y")
    if grain == "quarter":
        return f"Q{(start.month - 1) // 3 + 1} {start.year}"
    if grain == "year":
        return str(start.year)
    if start == end:
        return start.strftime("%d %B %Y")
    return f"{start.strftime('%d %b %Y')} to {end.strftime('%d %b %Y')}"


def days_in(start: date, end: date) -> int:
    """How many days a window spans, counting both ends."""
    return (end - start).days + 1
