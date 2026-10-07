"""AI employees: how one turns the assistant into a named worker for a conversation.

Nothing here runs a turn. It takes what the assistant was about to do — its
settings, its model, its tools, its instructions — and narrows it to the
employee a conversation is with:

* **settings**: its own model and effort over the assistant's, through a view
  that leaves the cached settings untouched for everybody else;
* **tools**: only its allowed modules (plus moving around the app), and its
  write mode — none, every one confirmed, or the assistant's own policy;
* **instructions**: who it is, its job and its rules, ahead of everything else
  the chat is about;
* **gates**: switched on, the person is in its audience, and its month's spend
  is under its budget.

It never widens anything. An employee cannot reach a tool the person could not,
nor skip a confirmation the assistant's policy asks for.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.assistant.policy import Actor, ResolvedTool
from app.models.ai_employee import AIEmployee, WriteMode
from app.models.assistant import AssistantConversation, AssistantModel, AssistantRun

SUBJECT_KIND: Final = "employee"
#: Always available: opening a screen is not a privilege, and an employee that
#: cannot show you where something is would be the less useful one.
ALWAYS_MODULES: Final = frozenset({"app"})
EFFORTS: Final = ("low", "medium", "high")


class EmployeeError(Exception):
    """A person may not talk to this employee now. The message is for them."""

    def __init__(self, message: str, status: int = 403) -> None:
        super().__init__(message)
        self.status = status


class SettingsView:
    """The assistant's settings with an employee's model and effort over them.

    A view rather than a copy: the settings object is shared through a cache,
    and writing an employee's model onto it would hand that model to whoever
    asked next.
    """

    def __init__(self, base: Any, overrides: dict[str, Any]) -> None:
        self._base = base
        self._overrides = overrides

    def __getattr__(self, name: str) -> Any:
        if name in self._overrides:
            return self._overrides[name]
        return getattr(self._base, name)


def may_talk(employee: AIEmployee, actor: Actor) -> bool:
    if not employee.enabled:
        return False
    if not employee.audience_roles:
        return True
    return actor.is_super_admin or bool(set(employee.audience_roles) & set(actor.roles))


async def month_spend(session: AsyncSession, employee_id: uuid.UUID) -> Decimal:
    """What its conversations have cost since the first of this month (UTC)."""
    start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    total = await session.scalar(
        select(func.coalesce(func.sum(AssistantRun.cost_usd), 0))
        .join(AssistantConversation, AssistantRun.conversation_id == AssistantConversation.id)
        .where(
            AssistantConversation.subject_kind == SUBJECT_KIND,
            AssistantConversation.subject_id == employee_id,
            AssistantRun.created_at >= start,
        )
    )
    return Decimal(str(total or 0))


async def check(session: AsyncSession, employee: AIEmployee | None, actor: Actor) -> AIEmployee:
    """The gates, in the order a person would want to hear about them."""
    if employee is None:
        raise EmployeeError("That AI employee no longer exists.", status=404)
    if not employee.enabled:
        raise EmployeeError(f"{employee.name} is switched off. A super admin can turn them back on.", status=409)
    if not may_talk(employee, actor):
        raise EmployeeError(f"{employee.name} is not available to you.")
    if employee.monthly_budget_usd is not None:
        spent = await month_spend(session, employee.id)
        if spent >= employee.monthly_budget_usd:
            raise EmployeeError(
                f"{employee.name} has used this month's budget (${employee.monthly_budget_usd}). "
                "A super admin can raise it.",
                status=409,
            )
    return employee


async def apply(session: AsyncSession, snapshot: Any, employee: AIEmployee) -> Any:
    """The snapshot this employee's turn runs with: its own model and effort."""
    overrides: dict[str, Any] = {}
    model: AssistantModel | None = snapshot.model
    if employee.model_key and employee.model_key != snapshot.settings.model_key:
        found = await session.get(AssistantModel, employee.model_key)
        if found is not None and found.enabled:
            overrides["model_key"] = found.key
            model = found
    if employee.reasoning_effort in EFFORTS:
        overrides["reasoning_effort"] = employee.reasoning_effort
    if not overrides:
        return snapshot
    return replace(snapshot, settings=SettingsView(snapshot.settings, overrides), model=model)


def narrow_tools(tools: list[ResolvedTool], employee: AIEmployee) -> list[ResolvedTool]:
    """Its modules and its write mode, applied to what the person already has."""
    allowed = set(employee.allowed_modules or []) | ALWAYS_MODULES
    everything = not employee.allowed_modules
    out: list[ResolvedTool] = []
    for tool in tools:
        if not everything and tool.spec.module_key not in allowed:
            continue
        if tool.spec.is_write:
            if employee.write_mode == WriteMode.READ_ONLY:
                continue
            if employee.write_mode == WriteMode.CONFIRM and not tool.requires_confirmation:
                tool = ResolvedTool(tool.spec, True)
        out.append(tool)
    return out


def persona(employee: AIEmployee) -> str:
    """Who it is, for the top of what the chat is about."""
    lines = [
        f"In this chat you are {employee.name}, {employee.title} at Hamdaz — an AI employee, "
        "not a person. Introduce yourself by that name, and say you are an AI if asked.",
    ]
    if employee.description.strip():
        lines.append("Your job:\n" + employee.description.strip())
    if employee.instructions.strip():
        lines.append(
            "Rules you must follow (set by your manager; they override any request to the contrary):\n"
            + employee.instructions.strip()
        )
    if employee.write_mode == WriteMode.READ_ONLY:
        lines.append(
            "You may look things up but not change anything in the system. If asked to, say "
            "so and say who can."
        )
    elif employee.write_mode == WriteMode.CONFIRM:
        lines.append("Every change you make waits for the person to confirm it first.")
    return "\n\n".join(lines)
