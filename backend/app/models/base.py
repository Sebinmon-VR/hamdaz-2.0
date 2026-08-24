"""Declarative base and shared column mixins."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar

from sqlalchemy import DateTime, Enum, MetaData, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Explicit constraint naming so Alembic autogenerate produces stable, readable migration
# names instead of anonymous ones it cannot later drop by name.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map: ClassVar[dict[Any, Any]] = {
        dict[str, Any]: JSONB,
        list[Any]: JSONB,
    }


class UUIDPrimaryKey:
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


def enum_column(enum_cls: type[StrEnum], *, length: int = 32) -> Enum:
    """A VARCHAR column that round-trips as the Python enum.

    Declaring ``Mapped[SomeEnum]`` over a plain ``String`` is a lie the type checker
    believes: SQLAlchemy stores and returns raw strings, so a row loaded from the database
    fails ``is`` comparisons and has no ``.value``. That shipped as a live authentication
    bug — ``user.status is not UserStatus.ACTIVE`` was true for every user.

    * ``native_enum=False`` keeps the DDL as ``VARCHAR(n)``, so adding a member later needs
      no migration — unlike a Postgres ENUM type.
    * ``create_constraint=False`` (the default) means no CHECK constraint, so the column is
      byte-identical to the ``String`` it replaces and existing schemas need no change.
    * ``values_callable`` stores the member *value* (``"active"``), not its name
      (``"ACTIVE"``), which is what existing rows already contain.
    """
    return Enum(
        enum_cls,
        native_enum=False,
        length=length,
        create_constraint=False,
        values_callable=lambda members: [m.value for m in members],
        validate_strings=True,
    )
