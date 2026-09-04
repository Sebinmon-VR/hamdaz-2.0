"""Request and response shapes for team dashboards."""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field


class WidgetInfo(BaseModel):
    key: str
    title: str
    description: str
    #: The module this belongs to; a team without it never sees the widget.
    module: str
    size: str
    #: Included when a team has no layout of its own.
    default: bool
    remote: bool


class PlacementOut(BaseModel):
    widget_key: str
    title: str
    module: str
    size: str
    position: int
    enabled: bool
    options: dict[str, Any]


class LayoutOut(BaseModel):
    team_id: uuid.UUID
    slug: str
    #: False when these are defaults rather than a saved arrangement.
    configured: bool
    widgets: list[PlacementOut]
    #: Everything this team could add, given the modules it holds.
    available: list[WidgetInfo]


class LayoutEntry(BaseModel):
    widget_key: str
    enabled: bool = True
    options: dict[str, Any] = Field(default_factory=dict)


class SetLayout(BaseModel):
    """The team's whole dashboard, in order. Replaces what was there."""

    widgets: list[LayoutEntry] = Field(default_factory=list)


class RenderedWidget(BaseModel):
    key: str
    title: str
    module: str
    size: str
    position: int
    options: dict[str, Any]
    #: Whatever the widget returned. Shape is the widget's own business.
    data: Any
    #: Set when this card failed; the rest of the dashboard still rendered.
    error: str | None
    elapsed_ms: int


class DashboardMeta(BaseModel):
    configured: bool
    #: Wall clock for the fan-out — the slowest card, not the sum.
    elapsed_ms: int
    widget_ms: dict[str, int]


class DashboardOut(BaseModel):
    team_id: uuid.UUID
    slug: str
    name: str
    widgets: list[RenderedWidget]
    errors: dict[str, str]
    meta: DashboardMeta
