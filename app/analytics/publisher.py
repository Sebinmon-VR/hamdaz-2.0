"""Writing the priority score to the ``useranalytics`` SharePoint list.

This is the one place the ranking leaves this database. Everything else in
``app.analytics`` reads SharePoint and keeps its answer in Postgres; this
takes the current standing of a team and rewrites one row per person in a
list on the Test site, so the tools that already read that list — a flow, a
sheet, a screen — see the same answer the app does.

Three rules, each because the list is shared:

* **Rows are matched by name and rewritten, never deleted.** The list is keyed
  by ``Username`` and other things may have added rows to it. Somebody who
  leaves the team keeps their row; it just stops being updated. A row that was
  never ours is never touched.
* **A row that has not changed is not written.** Every write bumps
  ``Modified`` on a row other tools sort by, and the live standing is
  recomputed every minute. Comparing before writing is what keeps the list's
  history meaningful — a modification there means the standing moved.
* **A failure is reported, never raised.** Publishing runs after a mirror
  sync and after an intake assignment, inside work that has to commit. A list
  that could not be reached is a line in the log and a field in the report,
  not a lost sync.

The columns are the list's, not ours, and were there before this module:

=================  ============================================================
``Title``          The person's name — the list's own key column.
``Username``       The same name. Existing rows are matched on this first.
``Priority``       **1 is next.** 0 means not in the queue at all — on leave,
                   a manager, or over the open-work ceiling.
``ActiveTasks``    Open rows whose bid has not closed. What the score is
                   built on.
``jobcount``       Every open row, closed bids included.
``RecentDate``     When they were last given work — the newest row assigned
                   to them.
``Leave``          ``Yes`` with the return date while on approved leave,
                   otherwise ``No``.
``Jobs``           The team, then the labels they hold, comma-separated.
``swapcounter``    Not written. Left for whatever set it.
=================  ============================================================
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.analytics import AnalyticsRun, LiveScore
from app.models.team import Team
from app.proposals.sharepoint import SharePointError, SharePointProposals

logger = logging.getLogger("hamdaz.analytics.publish")

#: The columns this module owns. ``swapcounter`` is deliberately absent.
COLUMNS: tuple[str, ...] = (
    "Title",
    "Username",
    "Priority",
    "ActiveTasks",
    "jobcount",
    "RecentDate",
    "Leave",
    "Jobs",
)


@dataclass(slots=True)
class Standing:
    """One person's position, in the shape the list wants.

    Built from either source of a ranking — the live table or a kept run —
    so the list is written the same way whichever produced it.
    """

    display_name: str
    rank: int
    active_tasks: int
    open_tasks: int
    last_assigned: date | None
    labels: list[str]
    team: str | None
    on_leave_until: str | None = None


@dataclass(slots=True)
class PublishReport:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    names: list[str] = field(default_factory=list)
    error: str | None = None
    reason: str = ""

    @property
    def written(self) -> int:
        return self.created + self.updated


# ── building standings ─────────────────────────────────────────────────


def _leave_until(labels: Iterable[str], reason: str | None) -> str | None:
    if "on-leave" not in set(labels):
        return None
    # "On approved leave until 2026-09-30" — keep the date, drop the prose.
    text = reason or ""
    return text.rsplit(" ", 1)[-1] if "until" in text else "yes"


def from_live(rows: Sequence[LiveScore], *, team: str | None) -> list[Standing]:
    out: list[Standing] = []
    for row in rows:
        labels = sorted(row.labels or [])
        # The live row keeps days rather than a date; turn it back into one so
        # the list shows something a person can read.
        last: date | None = None
        if row.days_since_assigned is not None:
            last = (row.computed_at or datetime.now(UTC)).date()
            last = date.fromordinal(last.toordinal() - int(row.days_since_assigned))
        out.append(
            Standing(
                display_name=row.display_name,
                rank=int(row.rank or 0) if row.eligible else 0,
                active_tasks=row.active_tasks,
                open_tasks=row.open_tasks,
                last_assigned=last,
                labels=labels,
                team=team,
                on_leave_until=_leave_until(labels, row.excluded_reason),
            )
        )
    return out


def from_run(record: AnalyticsRun, *, team: str | None = None) -> list[Standing]:
    """``team`` is the handle. Passed in because a run keeps the team's name,
    and the live path writes the handle — the two must not disagree, or every
    other publish would rewrite the Jobs column for no reason."""
    team = team or record.team_name
    out: list[Standing] = []
    for entry in record.entries:
        if entry.user_id is None:
            # Holds work in the list but has never signed in. Their row would
            # never be kept current by the live path, so it is not started.
            continue
        labels = list(entry.labels or [])
        out.append(
            Standing(
                display_name=entry.display_name,
                rank=0 if entry.excluded else int(entry.priority_score or 0),
                active_tasks=entry.active_tasks,
                open_tasks=entry.open_tasks,
                last_assigned=(
                    entry.last_assigned_on.date()
                    if isinstance(entry.last_assigned_on, datetime)
                    else entry.last_assigned_on
                ),
                labels=labels,
                team=team,
                on_leave_until=_leave_until(labels, entry.excluded_reason),
            )
        )
    return out


# ── the row ────────────────────────────────────────────────────────────


def fields_for(standing: Standing) -> dict[str, Any]:
    """What the row should say. Pure, so it can be checked without a list."""
    jobs = [standing.team.casefold()] if standing.team else []
    jobs += [label for label in standing.labels if label not in jobs]
    return {
        "Title": standing.display_name,
        "Username": standing.display_name,
        "Priority": standing.rank,
        "ActiveTasks": standing.active_tasks,
        "jobcount": standing.open_tasks,
        "RecentDate": (
            f"{standing.last_assigned:%Y-%m-%d}T00:00:00Z"
            if standing.last_assigned
            else None
        ),
        "Leave": (
            f"Yes, until {standing.on_leave_until}"
            if standing.on_leave_until and standing.on_leave_until != "yes"
            else ("Yes" if standing.on_leave_until else "No")
        ),
        "Jobs": ", ".join(jobs),
    }


def _same(current: dict[str, Any], wanted: dict[str, Any]) -> bool:
    """Whether writing ``wanted`` would change anything the list shows.

    Numbers come back from Graph as floats and dates with the time SharePoint
    chose, so the comparison is on what a person would see, not on bytes.
    """
    for key, value in wanted.items():
        have = current.get(key)
        if key == "RecentDate":
            if (str(have or "")[:10]) != (str(value or "")[:10]):
                return False
        elif isinstance(value, (int, float)):
            try:
                if float(have) != float(value):
                    return False
            except (TypeError, ValueError):
                return False
        elif (have or "") != (value or ""):
            return False
    return True


def _key(name: str | None) -> str:
    return " ".join((name or "").split()).casefold()


# ── the publisher ──────────────────────────────────────────────────────


class Publisher:
    """Knows the list, the switch, and how to rewrite a row."""

    def __init__(self, settings: Settings, sharepoint: SharePointProposals) -> None:
        self._settings = settings
        self._sharepoint = sharepoint

    @property
    def enabled(self) -> bool:
        return bool(
            self._settings.analytics_publish_enabled
            and self._settings.analytics_site_id
            and self._settings.analytics_list_id
        )

    @property
    def list_url(self) -> str:
        return self._settings.analytics_list_url

    async def publish(
        self, standings: Sequence[Standing], *, reason: str = ""
    ) -> PublishReport:
        """Rewrite the rows that moved. Never raises."""
        report = PublishReport(reason=reason)
        if not self.enabled:
            report.error = "Publishing is off (ANALYTICS_PUBLISH_ENABLED)"
            return report
        if not standings:
            return report

        site, lst = self._settings.analytics_site_id, self._settings.analytics_list_id
        try:
            items = await self._sharepoint.items_of(
                site, lst, select=",".join(COLUMNS)
            )
        except SharePointError as exc:
            report.error = f"Could not read the list: {exc}"
            logger.warning("publish: %s", report.error)
            return report

        # Matched on Username first, then Title. First row wins if the list
        # holds a duplicate — the others are left exactly as they are.
        by_name: dict[str, dict[str, Any]] = {}
        for item in items:
            fields = item.get("fields", {}) or {}
            for column in ("Username", "Title"):
                key = _key(fields.get(column))
                if key and key not in by_name:
                    by_name[key] = item

        for standing in standings:
            wanted = fields_for(standing)
            found = by_name.get(_key(standing.display_name))
            try:
                if found is None:
                    await self._sharepoint.create_item(site, lst, wanted)
                    report.created += 1
                    report.names.append(standing.display_name)
                elif _same(found.get("fields", {}) or {}, wanted):
                    report.unchanged += 1
                else:
                    await self._sharepoint.update_item(site, lst, str(found["id"]), wanted)
                    report.updated += 1
                    report.names.append(standing.display_name)
            except SharePointError as exc:
                # Keep going: one refused row should not stop the others.
                report.error = f"{standing.display_name}: {exc}"
                logger.warning("publish: %s", report.error)

        logger.info(
            "publish(%s): created=%d updated=%d unchanged=%d%s",
            reason, report.created, report.updated, report.unchanged,
            f" error={report.error}" if report.error else "",
        )
        return report


async def publish_team(
    session: AsyncSession, publisher: Publisher, team: Team, *, reason: str
) -> PublishReport:
    """Push one team's live standing, as it stands in the table right now."""
    from app.analytics import live

    rows = await live.standing(session, team=team)
    return await publisher.publish(from_live(rows, team=team.slug), reason=reason)


async def publish_live(
    session: AsyncSession, publisher: Publisher, *, reason: str
) -> list[PublishReport]:
    """Push the live standing of every team that distributes work this way.

    One report per team. A team is in scope when it has an enabled assignment
    policy of its own — the same rule the analytics screen uses — so giving a
    team a policy is also what starts its rows appearing in the list.
    """
    if not publisher.enabled:
        return []
    from sqlalchemy import select

    from app.models.assignment import AssignmentPolicy

    teams = (
        await session.scalars(
            select(Team)
            .join(AssignmentPolicy, AssignmentPolicy.team_id == Team.id)
            .where(AssignmentPolicy.enabled.is_(True), Team.archived_at.is_(None))
        )
    ).all()
    return [
        await publish_team(session, publisher, team, reason=reason) for team in teams
    ]
