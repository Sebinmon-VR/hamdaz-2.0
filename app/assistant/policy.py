"""Who may use the assistant, and which tools each person is shown.

Pure functions over already-loaded rows, so every rule here can be tested
without a database and reads the same however the call arrives.

Two questions, answered in order on every turn:

1. **Admission** — may this person talk to the assistant at all? The master
   switch, then the access rules, then the audience mode.
2. **Tool set** — of everything in the catalogue, what does this person get?
   The module's visibility gate, then the module policy, then the tool policy,
   then any role restriction the super admin added.

Neither answer is the security boundary. A tool call still goes through the
real route with the person's own session, and that route refuses whatever it
would refuse from a browser. What is decided here is what the model is *shown*,
which keeps it from attempting things it cannot do — and what the super admin
has switched off, which the route would otherwise happily allow.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from app.assistant.catalogue import (
    GROUPS_BY_KEY,
    LIVE_TOOLS,
    ModuleGroup,
    ToolSpec,
)
from app.models.assistant import (
    AssistantAccessRule,
    AssistantModel,
    AssistantModulePolicy,
    AssistantSettings,
    AssistantToolPolicy,
    AudienceMode,
    RuleEffect,
    SubjectType,
)
from app.roles.catalogue import ADMIN_ROLES, SUPER_ADMIN


@dataclass(frozen=True, slots=True)
class Actor:
    """Everything about the caller the policy needs, gathered once per turn."""

    user_id: uuid.UUID
    roles: frozenset[str]
    team_ids: frozenset[uuid.UUID]
    #: Module keys from the person's effective access.
    access_modules: frozenset[str]

    @property
    def is_super_admin(self) -> bool:
        return SUPER_ADMIN in self.roles

    @property
    def is_admin(self) -> bool:
        return not ADMIN_ROLES.isdisjoint(self.roles)


@dataclass(frozen=True, slots=True)
class Admission:
    admitted: bool
    #: Machine-readable, for the frontend to branch on.
    code: str | None = None
    #: Safe to show the person.
    reason: str | None = None


ADMITTED: Final = Admission(True)


def _rule_matches(rule: AssistantAccessRule, actor: Actor) -> bool:
    if rule.subject_type == SubjectType.USER:
        return rule.subject_id == str(actor.user_id)
    if rule.subject_type == SubjectType.TEAM:
        return rule.subject_id in {str(t) for t in actor.team_ids}
    if rule.subject_type == SubjectType.ROLE:
        return rule.subject_id in actor.roles
    return False


def admit(
    settings: AssistantSettings, rules: list[AssistantAccessRule], actor: Actor
) -> Admission:
    """The master switch, the block rules, then the audience mode.

    A super admin is always in the audience once the switch is on: they are the
    person configuring it, and being able to try it before releasing it is the
    point of the allow-list mode. Block always beats allow, so a rule that
    blocks a team wins over one that allows a person on it.
    """
    if not settings.enabled:
        return Admission(False, "disabled", "The assistant is switched off.")
    if actor.is_super_admin:
        return ADMITTED

    matching = [r for r in rules if r.enabled and _rule_matches(r, actor)]
    if any(r.effect == RuleEffect.BLOCK for r in matching):
        return Admission(False, "blocked", "The assistant is not available to you.")
    if settings.audience_mode == AudienceMode.ALLOW_LIST and not any(
        r.effect == RuleEffect.ALLOW for r in matching
    ):
        return Admission(False, "not_released", "The assistant has not been released to you yet.")
    return ADMITTED


@dataclass(frozen=True, slots=True)
class ResolvedTool:
    spec: ToolSpec
    requires_confirmation: bool

    @property
    def key(self) -> str:
        return self.spec.key


def _visible(group: ModuleGroup, actor: Actor) -> bool:
    if group.gate == "open":
        return True
    if group.gate == "access":
        return group.key in actor.access_modules
    return actor.is_admin


def effective_confirm(
    settings: AssistantSettings,
    module: AssistantModulePolicy | None,
    tool: AssistantToolPolicy | None,
) -> bool:
    """Tool override, else module, else the global default."""
    if tool is not None and tool.confirm_override is not None:
        return tool.confirm_override
    if module is not None and module.confirm_writes is not None:
        return module.confirm_writes
    return settings.confirm_writes_default


def effective_roles(
    module: AssistantModulePolicy | None, tool: AssistantToolPolicy | None
) -> list[str] | None:
    """The role restriction that applies: the tool's if it has one, else the module's."""
    if tool is not None and tool.allowed_roles:
        return list(tool.allowed_roles)
    if module is not None and module.allowed_roles:
        return list(module.allowed_roles)
    return None


def tool_enabled(
    spec: ToolSpec, module: AssistantModulePolicy | None, tool: AssistantToolPolicy | None
) -> bool:
    """Whether policy lets anyone use this tool, before looking at the person.

    A missing module policy — the seed not yet run — means reads on and writes
    off. A write that nobody has explicitly enabled must not run.
    """
    if tool is not None and not tool.enabled:
        return False
    if spec.is_write:
        return module is not None and module.write_enabled
    return module is None or module.read_enabled


def resolve_tools(
    settings: AssistantSettings,
    module_policies: dict[str, AssistantModulePolicy],
    tool_policies: dict[str, AssistantToolPolicy],
    actor: Actor,
) -> list[ResolvedTool]:
    """The tools this person is shown, in catalogue order.

    Planned tools are not in ``LIVE_TOOLS`` and so cannot appear here
    however the policy rows are set — which is the point of the
    distinction rather than a side effect of it.
    """
    out: list[ResolvedTool] = []
    for spec in LIVE_TOOLS:
        group = GROUPS_BY_KEY[spec.module_key]
        if not _visible(group, actor):
            continue
        module = module_policies.get(spec.module_key)
        tool = tool_policies.get(spec.key)
        if not tool_enabled(spec, module, tool):
            continue
        allowed = effective_roles(module, tool)
        if allowed and actor.roles.isdisjoint(allowed):
            continue
        out.append(
            ResolvedTool(
                spec,
                requires_confirmation=spec.is_write
                and effective_confirm(settings, module, tool),
            )
        )
    return out


_MILLION: Final = Decimal(1_000_000)


def cost_of(
    model: AssistantModel, *, input_tokens: int, cached_tokens: int, output_tokens: int
) -> Decimal:
    """USD for one model call, from the prices the super admin keeps.

    ``input_tokens`` as reported by OpenAI already includes the cached ones, so
    the cached count is taken out before the full price is applied.
    """
    uncached = max(input_tokens - cached_tokens, 0)
    total = (
        Decimal(uncached) * model.input_price
        + Decimal(cached_tokens) * model.cached_input_price
        + Decimal(output_tokens) * model.output_price
    )
    return (total / _MILLION).quantize(Decimal("0.000001"))
