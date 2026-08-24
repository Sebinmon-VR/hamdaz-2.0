"""Shared request/response shapes."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated

from fastapi import Query
from pydantic import BaseModel, Field

MAX_PAGE_SIZE = 200


class Page[T](BaseModel):
    """A page of results.

    Every list endpoint returns this. The legacy system loaded whole SharePoint lists into a
    pandas DataFrame on every request — root cause #3 and #5 in one line of code.
    """

    items: list[T]
    total: int
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total

    @classmethod
    def of(cls, items: Sequence[T], *, total: int, limit: int, offset: int) -> Page[T]:
        return cls(items=list(items), total=total, limit=limit, offset=offset)


class Pagination(BaseModel):
    limit: Annotated[int, Field(ge=1, le=MAX_PAGE_SIZE)] = 50
    offset: Annotated[int, Field(ge=0)] = 0


def pagination(
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Pagination:
    return Pagination(limit=limit, offset=offset)


class Message(BaseModel):
    message: str


class Created(BaseModel):
    id: str
    message: str = "Created"
