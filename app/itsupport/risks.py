"""Risk notes for the morning ask (feature risk_notes, your kept idea #2).

Two things worth a word next to a task in the morning ask, by rules:

* it closes within a day and has **no costing yet** (no quote request raised
  against the task);
* a supplier quote on its costing **runs out before the BCD**: its validity
  is free text ("30 days", "valid till 30/11/2026"), read by
  :func:`valid_until` from the quote's date, else the day it reached us.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.itsupport import when

_FORMATS: Final = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%b-%Y",
                   "%d-%b-%y", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%b %d %Y",
                   "%B %d %Y", "%d %b, %Y", "%d %B, %Y")
_DATE: Final = re.compile(
    r"\d{4}-\d{2}-\d{2}|\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}|"
    r"\d{1,2}[\s\-](?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[\s\-,]+\d{2,4}|"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2},?\s+\d{4}", re.I)
_SPAN: Final = re.compile(r"(\d{1,3})\s*(?:calendar\s+|working\s+)?(day|week|month)s?", re.I)


def parse_date(text: str | None) -> date | None:
    """A date as people write it here (day first)."""
    if not text:
        return None
    found = _DATE.search(text)
    if not found:
        return None
    raw = re.sub(r"\s+", " ", found.group(0).replace(",", ", ")).replace(" ,", ",").strip()
    for fmt in _FORMATS:
        for candidate in (raw, raw.replace(", ", " "), raw.title()):
            try:
                return datetime.strptime(candidate, fmt).date()
            except ValueError:
                continue
    return None


def valid_until(validity: str | None, quote_date: str | None,
                received: date | None) -> date | None:
    """The last day a supplier quote holds, from its validity text."""
    if not validity:
        return None
    if (span := _SPAN.search(validity)) is not None:
        start = parse_date(quote_date) or received
        if start is None:
            return None
        n, unit = int(span.group(1)), span.group(2).lower()
        days = n * {"day": 1, "week": 7, "month": 30}[unit]
        return start + timedelta(days=days)
    return parse_date(validity)


async def notes(session: AsyncSession, due_by_task: dict[str, datetime],
                now: datetime) -> dict[str, str]:
    """A short note per task that has a risk, by task id."""
    if not due_by_task:
        return {}
    from app.models.comparison import SupplierQuote
    from app.quoting.service import quotes_for_tasks

    quotes = await quotes_for_tasks(session, list(due_by_task))
    comparisons = {q.comparison_id for q in quotes.values() if q.comparison_id}
    supplier: dict[object, list[SupplierQuote]] = {}
    if comparisons:
        for sq in (await session.scalars(select(SupplierQuote).where(
                SupplierQuote.comparison_id.in_(comparisons)))).all():
            supplier.setdefault(sq.comparison_id, []).append(sq)
    out: dict[str, str] = {}
    for task_id, due in due_by_task.items():
        q = quotes.get(task_id)
        if q is None:
            if due - now <= timedelta(hours=24):
                out[task_id] = "no costing yet"
            continue
        closes = due.astimezone(when.UAE).date()
        for sq in supplier.get(q.comparison_id, []):
            until = valid_until(sq.validity, sq.quote_date,
                                sq.created_at.date() if sq.created_at else None)
            if until is not None and until < closes:
                out[task_id] = f"{sq.supplier_name}'s quote runs out {until:%a %d %b}"
                break
    return out
