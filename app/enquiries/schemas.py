"""What the enquiry analysis pages send and receive."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict


class TaskOut(BaseModel):
    id: str
    title: str
    end_user: str | None = None
    bid_closing_date: date | None = None


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source: str
    file_name: str
    path: str | None
    size: int | None
    web_url: str | None
    kind: str | None
    status: str
    note: str | None
    supplier_quote_id: uuid.UUID | None
    read_at: datetime | None
    created_at: datetime


class LineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    position: int
    description: str
    part_number: str | None
    brand: str | None
    quantity: Decimal | None
    unit: str | None
    specification: str | None
    source_document: str | None
    status: str
    history: list[dict[str, Any]] | None
    suppliers: list[dict[str, Any]] | None
    web: dict[str, Any] | None


class AnalysisOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    task_id: str
    task_title: str
    end_user: str | None
    bid_closing_date: date | None
    status: str
    stage: str | None
    error: str | None
    summary: str | None
    customer: str | None
    deadline: str | None
    conditions: list[str] | None
    missing: list[str] | None
    run_notes: list[str] | None
    #: The last run, step by step: ``[{at, level, message}]``.
    run_log: list[dict[str, str]] | None = None
    web_search: bool
    model: str | None
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    started_at: datetime | None
    finished_at: datetime | None
    drive_folder: str | None
    drive_folder_url: str | None
    report_pdf_url: str | None
    report_xlsx_url: str | None
    filing_error: str | None
    comparison_id: uuid.UUID | None
    run_by_name: str | None = None
    counts: dict[str, int]
    documents: list[DocumentOut]
    lines: list[LineOut]


class TaskAnalysisOut(BaseModel):
    task: TaskOut
    analysis: AnalysisOut | None
    #: Whether a run can be started: the key for reading is set.
    can_run: bool
    web_search_default: bool


class SummaryOut(BaseModel):
    task_id: str
    task_title: str
    end_user: str | None
    bid_closing_date: date | None
    status: str
    items: int
    recent: int
    history: int
    new: int
    finished_at: datetime | None
    run_by_name: str | None


class ListOut(BaseModel):
    analyses: list[SummaryOut]


class RunIn(BaseModel):
    #: Look new items up on the web this run. Omitted: the configured default.
    web_search: bool | None = None
