from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AgentIn(BaseModel):
    #: The browser makes an id for a new agent, so a draft keeps its address
    #: when it is first saved. Ignored when it is not a fresh UUID.
    id: uuid.UUID | None = None
    name: str = Field(min_length=1, max_length=80)
    role: str = Field(default="", max_length=200)
    avatar: dict[str, Any] = Field(default_factory=dict)
    instructions: str = Field(default="", max_length=20_000)
    steps: list[dict[str, Any]] = Field(default_factory=list)


class AgentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    role: str
    avatar: dict[str, Any]
    instructions: str
    steps: list[dict[str, Any]]
    created_at: datetime
    updated_at: datetime


class RunSummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    agent_id: uuid.UUID
    task_id: str | None
    task_title: str | None
    status: str
    summary: str | None
    cost_usd: Decimal
    created_at: datetime
    finished_at: datetime | None


class RecentRunOut(RunSummaryOut):
    """A run in the list across all of somebody's agents."""

    agent_name: str
    agent_avatar: dict[str, Any]
    question_title: str | None = None


class RunOut(RunSummaryOut):
    plan: dict[str, Any]
    results: list[dict[str, Any]]
    question: dict[str, Any] | None
    error: str | None
    model: str | None
    input_tokens: int
    output_tokens: int
    web_searches: int
    started_at: datetime | None


class AnswerIn(BaseModel):
    go: bool = True
    text: str = Field(default="", max_length=4000)


class TaskOut(BaseModel):
    id: str
    title: str
    end_user: str | None
    deadline: date | None
    status: str | None


class StatusOut(BaseModel):
    """What the run panel says about the model, before anybody presses Run."""

    model: str
    effort: str
    configured: bool
    web_allowed: bool


class Turn(BaseModel):
    role: str = Field(pattern="^(you|agent)$")
    text: str = Field(max_length=4000)


class AroundIn(BaseModel):
    """What surrounds a boxed part of the screen, as the browser read it."""

    column: str = Field(default="", max_length=300)
    row: str = Field(default="", max_length=1000)
    section: str = Field(default="", max_length=300)
    currencies: list[str] = Field(default_factory=list, max_length=10)
    page_text: str = Field(default="", max_length=8000)


class AssistIn(BaseModel):
    """Anything typed into the companion's bubble, with what it was looking at."""

    agent_id: uuid.UUID | None = None
    page: str = Field(default="", max_length=200)
    selection: str = Field(default="", max_length=8000)
    around: AroundIn = Field(default_factory=AroundIn)
    question: str = Field(default="", max_length=2000)
    history: list[Turn] = Field(default_factory=list, max_length=20)


class AssistOut(BaseModel):
    answer: str
    #: glossary / system — the app itself, free; free — a free model; ai —
    #: the paid fallback; busy — no model would answer; guide — start a
    #: walk-through towards ``guide_goal`` instead.
    how: str
    #: Which model answered, e.g. "groq:openai/gpt-oss-120b".
    model: str | None = None
    cost_usd: Decimal = Decimal(0)
    guide_goal: str | None = None


class TeamsAnswerIn(BaseModel):
    """What the Power Automate flow sends back after the owner answers in Teams.

    ``go`` arrives as whatever the card's button carried — true/false, or the
    strings "true"/"false" when the flow builds the body as text.
    """

    run_id: uuid.UUID
    step_id: str = Field(max_length=64)
    token: str = Field(max_length=128)
    go: bool | str = True
    text: str = Field(default="", max_length=4000)
    #: Who pressed the button, from the response's responder. Checked when given.
    responder: str = Field(default="", max_length=320)


class TeamsAnswerOut(BaseModel):
    ok: bool
    #: For the flow to put on the card once it is answered.
    message: str


class GuideElement(BaseModel):
    id: str = Field(max_length=20)
    label: str = Field(max_length=120)
    kind: str = Field(default="button", max_length=20)


class GuidePage(BaseModel):
    path: str = Field(max_length=200)
    label: str = Field(max_length=120)


class GuideIn(BaseModel):
    goal: str = Field(min_length=1, max_length=500)
    path: str = Field(max_length=300)
    page: str = Field(default="", max_length=200)
    elements: list[GuideElement] = Field(default_factory=list, max_length=200)
    pages: list[GuidePage] = Field(default_factory=list, max_length=150)
    #: Element ids clicked and "nav:<path>" for pages visited, so far.
    done: list[str] = Field(default_factory=list, max_length=40)


class GuideOut(BaseModel):
    #: click — point at ``target``; navigate — go to ``path``; done — finished.
    action: str
    say: str
    target: str | None = None
    path: str | None = None
    #: system — a recipe or a page name, free; free — a free model; busy.
    how: str
