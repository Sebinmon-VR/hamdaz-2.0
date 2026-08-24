"""Platform tables backing the audit trail and the developer panel (§6, §7).

``audit_log`` answers *who did this?* Root cause #8 is that the legacy system cannot answer
it at all — there is no action log anywhere in 7,900 lines.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamped, UUIDPrimaryKey, enum_column


class AuditAction(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    LOGIN = "login"
    LOGOUT = "logout"
    APPROVE = "approve"
    REJECT = "reject"
    ASSIGN = "assign"
    PUBLISH = "publish"


class AuditLog(Base, UUIDPrimaryKey):
    """Append-only. Never updated, never deleted from application code."""

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_entity", "entity_type", "entity_id"),
        Index("ix_audit_log_actor_created", "actor_id", "created_at"),
    )

    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="SET NULL"), index=True
    )
    action: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(60), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(64))
    #: Before/after snapshots. Secrets are redacted by the audit writer, not here.
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    ip: Mapped[str | None] = mapped_column(String(45))  # fits IPv6
    user_agent: Mapped[str | None] = mapped_column(Text)
    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    def __repr__(self) -> str:
        return f"<AuditLog {self.action} {self.entity_type}>"


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    RETRYING = "retrying"
    CANCELLED = "cancelled"


class JobRun(Base, UUIDPrimaryKey):
    """One execution of a Celery task, as rendered by the developer panel's job screen."""

    __tablename__ = "job_runs"
    __table_args__ = (Index("ix_job_runs_task_started", "task_name", "started_at"),)

    task_name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    task_id: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    args: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[JobStatus] = mapped_column(
        enum_column(JobStatus, length=16), nullable=False, index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    retries: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:
        return f"<JobRun {self.task_name} {self.status}>"


class ConnectorMode(StrEnum):
    READ_ONLY = "read_only"
    #: Writes permitted, but confined to the sandbox site. See §8.1.
    READ_WRITE_SANDBOX = "read_write_sandbox"
    READ_WRITE = "read_write"


class ConnectorStatus(Base, Timestamped):
    """Health and sync state per connector, surfaced in both panels.

    The delta cursor lives here rather than in a module global — the legacy system kept it in
    ``delta_link`` at module scope, which is why every worker resynced independently.
    """

    __tablename__ = "connector_status"

    name: Mapped[str] = mapped_column(String(60), primary_key=True)
    mode: Mapped[ConnectorMode] = mapped_column(
        enum_column(ConnectorMode, length=24), nullable=False
    )
    healthy: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    #: Opaque per-connector sync state, e.g. ``{"delta_link": "..."}``.
    cursor: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    def __repr__(self) -> str:
        return f"<ConnectorStatus {self.name} {self.mode}>"


class FeatureFlag(Base, Timestamped):
    __tablename__ = "feature_flags"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    description: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: Targeting rules, e.g. ``{"teams": [...], "users": [...], "percentage": 25}``.
    rules: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    def __repr__(self) -> str:
        return f"<FeatureFlag {self.key}>"


class OutboxStatus(StrEnum):
    CAPTURED = "captured"
    SENT = "sent"
    FAILED = "failed"


class EmailOutbox(Base, UUIDPrimaryKey):
    """Captured outbound mail (§8.2).

    Outside production, ``outbound_email_enabled`` is false and every message lands here
    instead of going out. A development build must not email a real supplier.
    """

    __tablename__ = "email_outbox"

    to_addresses: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    cc_addresses: Mapped[list[Any] | None] = mapped_column(JSONB)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    sent_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    status: Mapped[OutboxStatus] = mapped_column(
        enum_column(OutboxStatus, length=16),
        default=OutboxStatus.CAPTURED,
        nullable=False,
        index=True,
    )
    error: Mapped[str | None] = mapped_column(Text)
    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        return f"<EmailOutbox {self.subject!r} {self.status}>"
