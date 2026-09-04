"""The user record.

Microsoft is the only identity provider, so there is deliberately no password,
no password hash, and no local credential of any kind. Authentication happens at
Entra ID; this table exists so the rest of the ERP has a stable internal id to
hang records off, and so a person can be disabled here without touching Entra.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamped, UUIDPrimaryKey


class User(Base, UUIDPrimaryKey, Timestamped):
    __tablename__ = "users"

    #: Entra ID object id (the ``oid`` claim) — the only stable identifier a
    #: person has. Email addresses get changed on marriage, rebrand or typo fix;
    #: this does not. Matching on email instead is how duplicate accounts happen.
    entra_object_id: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)

    #: Local kill switch. Entra can still authenticate them; we refuse the session.
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: When they joined the company, for the new-joiner rule.
    #:
    #: Kept here rather than read from Entra: ``employeeHireDate`` needs a
    #: permission this app does not hold and is unset on most tenants anyway.
    #: Unset, the assignment policy falls back to ``created_at`` — first seen in
    #: this system, which is close enough to be useful and wrong enough to be
    #: worth correcting, so it is editable.
    joined_on: Mapped[date | None] = mapped_column(Date)

    def __repr__(self) -> str:
        return f"<User {self.email}>"
