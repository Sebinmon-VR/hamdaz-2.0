"""Each team's dashboard layout.

A row per widget a team has arranged. No rows at all means the team has never
been configured and gets sensible defaults derived from the modules it holds —
so a brand-new team has a working dashboard without anyone touching it.

Widget keys are plain strings rather than a foreign key: widgets live in code,
not in a table, and a layout should survive a widget being renamed or removed
without a migration. Unknown keys are ignored at render time.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Boolean, ForeignKey, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamped, UUIDPrimaryKey


class TeamDashboardWidget(Base, UUIDPrimaryKey, Timestamped):
    __tablename__ = "team_dashboard_widgets"
    __table_args__ = (
        UniqueConstraint("team_id", "widget_key", name="uq_team_dashboard_widget"),
    )

    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True
    )
    widget_key: Mapped[str] = mapped_column(String(40), nullable=False)
    #: Order on the page. Gaps are fine; only the relative order matters.
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Kept but hidden, so turning a card back on restores its settings.
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    #: Per-team widget settings, e.g. {"limit": 5}.
    options: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    configured_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    def __repr__(self) -> str:
        return f"<TeamDashboardWidget team={self.team_id} {self.widget_key}>"
