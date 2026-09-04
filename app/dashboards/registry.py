"""The widget registry each module plugs its dashboard cards into.

A widget declares which module it belongs to, and that is the whole trick: a
team only ever sees widgets for modules it has actually been granted. Module
visibility and dashboard content cannot drift apart, because they are the same
decision expressed once.

Widgets are loaded concurrently, each with its own session, for the same reason
profile sections are — the database is a third of a second away and the cards
have no ordering between them.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.team import Team
from app.models.user import User

#: A layout hint for the frontend, not a rule the backend enforces.
WidgetSize = Literal["small", "medium", "large", "full"]


@dataclass(slots=True)
class WidgetContext:
    #: The team whose dashboard is being rendered.
    team: Team
    #: Who is looking. Lets a widget answer "what is *my* role here".
    viewer: User
    #: Private to this widget, so the fan-out is safe.
    session: AsyncSession
    directory: Any
    #: Per-team settings from the layout row, e.g. how many rows to show.
    options: dict[str, Any]
    #: Read-only SharePoint client, for widgets that need it. None when the
    #: caller did not supply one, which a widget must tolerate.
    sharepoint: Any = None
    #: Shared aggregate cache, for cards that summarise the whole list.
    workload_cache: Any = None


@dataclass(frozen=True, slots=True)
class Widget:
    key: str
    title: str
    description: str
    #: The module this belongs to. A team without that module never sees it.
    module: str
    load: Callable[[WidgetContext], Awaitable[Any]]
    size: WidgetSize = "medium"
    #: Included when a team has no layout of its own.
    default: bool = False
    #: Reaches the network; skipped when only local data is wanted.
    remote: bool = False


_WIDGETS: dict[str, Widget] = {}


def register(widget: Widget) -> Widget:
    if widget.key in _WIDGETS:
        raise ValueError(f"widget {widget.key!r} is already registered")
    _WIDGETS[widget.key] = widget
    return widget


def all_widgets() -> list[Widget]:
    return list(_WIDGETS.values())


def get(key: str) -> Widget | None:
    return _WIDGETS.get(key)


def for_modules(module_keys: set[str]) -> list[Widget]:
    """Widgets a team may have, given the modules it has been granted."""
    return [w for w in _WIDGETS.values() if w.module in module_keys]


def defaults_for(module_keys: set[str]) -> list[Widget]:
    return [w for w in for_modules(module_keys) if w.default]
