"""Keep the "BCD UAE Time" of each Proposals row in line with Ariba.

After a visit, every open event is matched to the Proposals row carrying its
tender number, and the row's BCD is compared with Ariba's End Time. Only a row
that disagrees is written, and only the BCD column.

**What "agrees" means.** People type the UAE time into BCD, and SharePoint
stores it as the site's own zone's time (``sharepoint_site_timezone``) — a UAE noon
is 19:00 UTC in summer and 20:00 in winter. That is what the list shows people
and what they expect, so the correct value is the UAE wall clock read in the
site's zone; comparing true instants would "correct" nearly every row into one
that looks eleven hours wrong. Compared to the minute.

**Guarded twice.** ``ariba_fix_bcd`` off means nothing is written: the
differences are kept as a preview. And a tender number carried by more than
one live row is not written either — which of them is the tender is a person's
call.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ariba.service import OPEN, match
from app.core.config import Settings
from app.models.ariba import AribaBcdFix, AribaEvent
from app.proposals.sharepoint import SharePointError, SharePointProposals

_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def expected_bcd(end_time: datetime, *, uae: str, site: str) -> str:
    """The BCD value that shows Ariba's End Time as UAE time in the list."""
    wall = end_time.astimezone(ZoneInfo(uae)).replace(tzinfo=None)
    return wall.replace(tzinfo=ZoneInfo(site)).astimezone(UTC).strftime(_FORMAT)


def same_minute(raw: str | None, expected: str) -> bool:
    if not raw:
        return False
    try:
        held = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return False
    want = datetime.strptime(expected, _FORMAT).replace(tzinfo=UTC)
    return held.replace(second=0, microsecond=0) == want.replace(second=0, microsecond=0)


@dataclass(slots=True)
class CheckReport:
    compared: int = 0
    differing: int = 0
    written: int = 0
    failed: int = 0
    ambiguous: int = 0

    def summary(self, *, writing: bool) -> str:
        if not self.differing:
            return f"BCD: {self.compared} compared, all match"
        done = (
            f"{self.written} corrected" + (f", {self.failed} failed" if self.failed else "")
            if writing
            else "preview only"
        )
        extra = f", {self.ambiguous} on duplicate rows left alone" if self.ambiguous else ""
        return f"BCD: {self.compared} compared, {self.differing} differ — {done}{extra}"


async def check(
    session: AsyncSession, settings: Settings, sharepoint: SharePointProposals
) -> CheckReport:
    """Compare every open event with its Proposals row, and correct if allowed.

    One read of the list (two columns), then one write per differing row.
    """
    report = CheckReport()
    events = (
        await session.scalars(
            select(AribaEvent).where(
                AribaEvent.status == OPEN,
                AribaEvent.end_time.is_not(None),
            )
        )
    ).all()
    if not events:
        return report

    tasks = await sharepoint.all_tasks(fields="Title,BCD")
    matched = match(settings, events, tasks)

    # A preview is a statement about now; the last one is replaced.
    await session.execute(delete(AribaBcdFix).where(AribaBcdFix.applied.is_(False)))

    writing = settings.ariba_fix_bcd
    for event in events:
        rows = matched.get(event.doc_id, [])
        if not rows:
            continue
        want = expected_bcd(
            event.end_time,
            uae=settings.sharepoint_meant_timezone,
            site=settings.sharepoint_site_timezone,
        )
        for task in rows:
            report.compared += 1
            if same_minute(task.bid_closing_date, want):
                continue
            report.differing += 1
            fix = AribaBcdFix(
                item_id=str(task.id),
                doc_id=event.doc_id,
                reference=event.reference or event.doc_id,
                task_title=task.title or "",
                old_bcd=task.bid_closing_date,
                new_bcd=want,
                ariba_end_time=event.end_time,
                applied=False,
            )
            if len(rows) > 1:
                report.ambiguous += 1
                fix.error = f"{len(rows)} Proposals rows carry {event.reference}; not changed"
            elif writing:
                try:
                    await sharepoint.update_task(str(task.id), {"BCD": want})
                    fix.applied = True
                    report.written += 1
                except SharePointError as exc:
                    fix.error = str(exc)[:600]
                    report.failed += 1
            session.add(fix)
    await session.flush()
    return report
