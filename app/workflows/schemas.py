"""Request and response shapes for workflows."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# ── the blocks and the flows ───────────────────────────────────────────


class ConfigFieldOut(BaseModel):
    key: str
    label: str
    type: str
    required: bool
    help: str
    options: list[str]
    default: Any = None


class BlockOut(BaseModel):
    kind: str
    name: str
    description: str
    waits: str | None
    switch: str | None
    fields: list[ConfigFieldOut]


class ToolChoiceOut(BaseModel):
    key: str
    label: str
    module: str
    method: str
    path: str


class BlocksOut(BaseModel):
    """Everything the builder needs: the blocks, and the routes an endpoint block may call."""

    blocks: list[BlockOut]
    tools: list[ToolChoiceOut]
    schemas: list[str]


class StepIn(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    kind: str = Field(min_length=1, max_length=40)
    name: str | None = Field(default=None, max_length=160)
    config: dict[str, Any] = Field(default_factory=dict)
    when: dict[str, Any] | None = None


class StepOut(BaseModel):
    key: str
    kind: str
    name: str
    config: dict[str, Any]
    when: dict[str, Any] | None


class WorkflowIn(BaseModel):
    key: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: str = Field(min_length=1, max_length=160)
    description: str | None = None
    team: str | None = Field(default=None, description="Team handle or id; empty for any team.")
    trigger: str = Field(default="manual", pattern=r"^(manual|task_assigned)$")
    enabled: bool = True
    steps: list[StepIn] = Field(min_length=1)


class WorkflowPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = None
    team: str | None = None
    trigger: str | None = Field(default=None, pattern=r"^(manual|task_assigned)$")
    enabled: bool | None = None
    steps: list[StepIn] | None = None


class WorkflowOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    key: str
    name: str
    description: str | None
    team_id: uuid.UUID | None
    team_slug: str | None = None
    team_name: str | None = None
    subject_kind: str
    trigger: str
    enabled: bool
    version: int
    is_system: bool
    archived_at: datetime | None
    steps: list[StepOut]
    #: Runs still going on this flow, for the list.
    open_runs: int = 0


class SettingsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    send_email: bool
    write_sharepoint: bool
    write_zoho: bool
    from_mailbox: str | None
    poll_seconds: int


class SettingsIn(BaseModel):
    send_email: bool | None = None
    write_sharepoint: bool | None = None
    write_zoho: bool | None = None
    from_mailbox: str | None = Field(default=None, max_length=320)
    poll_seconds: int | None = Field(default=None, ge=30, le=3600)


# ── runs ───────────────────────────────────────────────────────────────


class StartRunIn(BaseModel):
    #: The Proposals task id.
    subject_id: str = Field(min_length=1, max_length=120)
    subject_label: str | None = Field(default=None, max_length=300)


class AnswerPair(BaseModel):
    key: str = Field(min_length=1, max_length=80)
    value: Any = None


class AnswerIn(BaseModel):
    """What the person answered on a waiting step.

    ``values`` for a form; ``value`` for a review, when they edited what was
    shown (leave it out to verify as shown). Files go through the upload
    route first and are matched to the step by their order of arrival.
    """

    values: dict[str, Any] = Field(default_factory=dict)
    value: Any = None
    #: The same answer as key/value pairs, for a caller that cannot send an
    #: open-keyed object — the assistant's strict tool schemas cannot. Merged
    #: into ``values``.
    pairs: list[AnswerPair] = Field(default_factory=list)
    #: ``value`` as JSON text, for the same caller.
    value_json: str | None = Field(default=None, max_length=200_000)

    def merged(self) -> tuple[dict[str, Any], Any]:
        import json

        values = dict(self.values)
        for pair in self.pairs:
            values[pair.key] = pair.value
        value = self.value
        if value is None and self.value_json:
            try:
                value = json.loads(self.value_json)
            except ValueError:
                value = self.value_json
        return values, value


class RunEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    seq: int
    kind: str
    step_key: str | None
    payload: dict[str, Any] | None
    by_user_id: uuid.UUID | None
    created_at: datetime


class RunFileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    step_key: str | None
    source: str
    file_name: str
    content_type: str | None
    size: int
    origin: str | None
    created_at: datetime


class RunMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    step_key: str | None
    direction: str
    party: str | None
    address: str | None
    subject: str | None
    body: str | None
    state: str
    error: str | None
    sent_at: datetime | None
    received_at: datetime | None


class RunStepOut(BaseModel):
    """One step as the run sees it: the definition plus where the run is on it."""

    key: str
    kind: str
    name: str
    #: pending, running, waiting, done, skipped, failed
    state: str
    note: str | None = None


class RunSummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workflow_id: uuid.UUID
    workflow_key: str
    workflow_name: str
    subject_kind: str
    subject_id: str
    subject_label: str | None
    owner_id: uuid.UUID
    owner_name: str
    team_id: uuid.UUID | None
    tag: str
    status: str
    step_index: int
    step_count: int
    current_step: str | None
    #: The pending question's title, when waiting on the person.
    waiting_for: str | None
    started_at: datetime
    finished_at: datetime | None
    error: str | None
    cost_usd: Decimal


class RunOut(RunSummaryOut):
    context: dict[str, Any]
    pending: dict[str, Any] | None
    wake_at: datetime | None
    deadline_at: datetime | None
    steps: list[RunStepOut]
    events: list[RunEventOut]
    files: list[RunFileOut]
    messages: list[RunMessageOut]


class TaskRunsOut(BaseModel):
    """What a task page shows: the flows this person may start, and the runs already on the task."""

    workflows: list[WorkflowOut]
    runs: list[RunSummaryOut]
