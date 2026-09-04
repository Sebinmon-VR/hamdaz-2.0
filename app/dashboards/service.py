"""Resolving a team's dashboard layout, and rendering it.

Layout resolution has one rule worth stating plainly: **a widget is only ever
shown if the team still holds its module.** A team can be configured with a
widget and later lose that module; the card disappears without anyone editing
the layout. Visibility is the single source of truth, and the saved layout is a
preference on top of it, never a way around it.

Rendering fans the widgets out concurrently, each on its own session, for the
same reason profile sections do.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.access import service as access_service
from app.dashboards import registry
from app.dashboards.registry import Widget, WidgetContext
from app.models.dashboard import TeamDashboardWidget
from app.models.team import Team
from app.models.user import User


class DashboardError(Exception):
    """A dashboard operation was refused. Safe to show a user."""


class DashboardNotFoundError(DashboardError):
    pass


@dataclass(slots=True)
class Placement:
    widget: Widget
    position: int
    enabled: bool
    options: dict[str, Any]
    #: False when this came from defaults rather than a saved row.
    configured: bool


async def granted_module_keys(session: AsyncSession, team_id: uuid.UUID) -> set[str]:
    grants = await access_service.team_access(session, team_id)
    return {g.module_key for g in grants}


async def resolve_layout(session: AsyncSession, team: Team) -> list[Placement]:
    """The widgets this team's dashboard shows, in order.

    Falls back to defaults when nothing has been configured, and filters
    everything through the team's current module grants.
    """
    modules = await granted_module_keys(session, team.id)
    available = {w.key: w for w in registry.for_modules(modules)}

    rows = list(
        (
            await session.scalars(
                select(TeamDashboardWidget)
                .where(TeamDashboardWidget.team_id == team.id)
                .order_by(TeamDashboardWidget.position)
            )
        ).all()
    )

    if not rows:
        return [
            Placement(widget=w, position=i, enabled=True, options={}, configured=False)
            for i, w in enumerate(registry.defaults_for(modules))
        ]

    placements: list[Placement] = []
    for row in rows:
        widget = available.get(row.widget_key)
        # Either the widget was removed from the code, or the team lost the
        # module it belongs to. Both mean: do not show it.
        if widget is None:
            continue
        placements.append(
            Placement(
                widget=widget,
                position=row.position,
                enabled=row.enabled,
                options=dict(row.options or {}),
                configured=True,
            )
        )
    placements.sort(key=lambda p: p.position)
    return placements


async def _render_widget(
    placement: Placement,
    team: Team,
    viewer: User,
    factory: async_sessionmaker[AsyncSession],
    directory: Any,
    sharepoint: Any = None,
    workload_cache: Any = None,
) -> dict[str, Any]:
    started = time.monotonic()
    data: Any = None
    error: str | None = None
    try:
        async with factory() as session:
            data = await placement.widget.load(
                WidgetContext(
                    team=team,
                    viewer=viewer,
                    session=session,
                    directory=directory,
                    options=placement.options,
                    sharepoint=sharepoint,
                    workload_cache=workload_cache,
                )
            )
    except Exception as exc:  # noqa: BLE001 - one bad card must not blank the page
        error = f"{type(exc).__name__}: {exc}"

    return {
        "key": placement.widget.key,
        "title": placement.widget.title,
        "size": placement.widget.size,
        "module": placement.widget.module,
        "position": placement.position,
        "options": placement.options,
        "data": data,
        "error": error,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }


async def render(
    *,
    team: Team,
    viewer: User,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    directory: Any,
    sharepoint: Any = None,
    workload_cache: Any = None,
    include_remote: bool = True,
) -> dict[str, Any]:
    placements = [p for p in await resolve_layout(session, team) if p.enabled]
    if not include_remote:
        placements = [p for p in placements if not p.widget.remote]

    started = time.monotonic()
    widgets = await asyncio.gather(
        *(
            _render_widget(
                p, team, viewer, factory, directory, sharepoint, workload_cache
            )
            for p in placements
        )
    )
    total_ms = int((time.monotonic() - started) * 1000)

    return {
        "team_id": str(team.id),
        "slug": team.slug,
        "name": team.name,
        "widgets": list(widgets),
        "errors": {w["key"]: w["error"] for w in widgets if w["error"]},
        "meta": {
            "configured": any(p.configured for p in placements),
            "elapsed_ms": total_ms,
            "widget_ms": {w["key"]: w["elapsed_ms"] for w in widgets},
        },
    }


# ── configuring ────────────────────────────────────────────────────────


async def set_layout(
    session: AsyncSession,
    *,
    team: Team,
    entries: list[dict[str, Any]],
    configured_by_id: uuid.UUID | None = None,
) -> list[Placement]:
    """Replace the team's layout with ``entries``.

    Each entry is ``{"widget_key", "enabled"?, "options"?}``; order in the list
    is the order on the page. Replacing wholesale means a save cannot half-apply.
    """
    modules = await granted_module_keys(session, team.id)
    available = {w.key: w for w in registry.for_modules(modules)}

    seen: set[str] = set()
    for entry in entries:
        key = entry.get("widget_key")
        if not key:
            raise DashboardError("Every entry needs a widget_key")
        if key in seen:
            raise DashboardError(f"{key!r} appears more than once")
        seen.add(key)

        if registry.get(key) is None:
            raise DashboardNotFoundError(f"No widget named {key!r}")
        if key not in available:
            # Refused rather than silently dropped: saving a layout that quietly
            # loses a card is worse than being told why.
            widget = registry.get(key)
            raise DashboardError(
                f"{key!r} belongs to the {widget.module!r} module, which this team "
                f"has not been granted"
            )

    await session.execute(
        delete(TeamDashboardWidget).where(TeamDashboardWidget.team_id == team.id)
    )
    await session.flush()

    for position, entry in enumerate(entries):
        session.add(
            TeamDashboardWidget(
                team_id=team.id,
                widget_key=entry["widget_key"],
                position=position,
                enabled=bool(entry.get("enabled", True)),
                options=dict(entry.get("options") or {}),
                configured_by_id=configured_by_id,
            )
        )
    await session.flush()
    return await resolve_layout(session, team)


async def reset_layout(session: AsyncSession, *, team: Team) -> list[Placement]:
    """Drop the saved layout so the team falls back to defaults."""
    await session.execute(
        delete(TeamDashboardWidget).where(TeamDashboardWidget.team_id == team.id)
    )
    await session.flush()
    return await resolve_layout(session, team)


async def available_widgets(session: AsyncSession, team: Team) -> list[Widget]:
    """Widgets this team could use, given the modules it holds."""
    return registry.for_modules(await granted_module_keys(session, team.id))
