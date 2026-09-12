"""Who may use the assistant, and which tools each person is shown.

Pure functions over already-loaded rows, so every rule here can be tested
without a database and reads the same however the call arrives.

Two questions, answered in order on every turn:

1. **Admission** — may this person talk to the assistant at all? The master
   switch, then the access rules, then the audience mode.
2. **Tool set** — of everything in the catalogue, what does this person get?
   The module's visibility gate, then the module policy, then the tool policy,
   then any role restriction the super admin added — and for a write, the
   separate question of who may write there.

Those last two are deliberately two questions. Everyone reads the team list;
only some people may have the assistant delete a team. Answering both from one
list of roles would mean the only way to stop somebody deleting a team was to
stop them looking at teams, and an access model that forces that trade stops
being used as intended within a week.

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
    DELETE_ROLES,
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
    AssistantVoiceModel,
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


def effective_write_roles(
    module: AssistantModulePolicy | None, tool: AssistantToolPolicy | None
) -> list[str] | None:
    """Who may have the assistant *write* here. Tool's if set, else module's.

    Resolved the same way as ``effective_roles`` and kept a separate function
    because it answers a separate question. ``effective_roles`` decides who sees
    the module at all; this decides who may change what they can see. An
    ordinary employee reading the team list and not being able to delete a team
    is one person matching the first and not the second, which is the ordinary
    case rather than the exotic one.

    Only ever consulted for a write. A read is never held back by it, however
    it is set — including by an administrator who sets it on a module that has
    no writes at all, which is a reasonable thing to do before one exists.
    """
    if tool is not None and tool.write_roles:
        return list(tool.write_roles)
    if module is not None and module.write_roles:
        return list(module.write_roles)
    return None


def tool_enabled(
    spec: ToolSpec, module: AssistantModulePolicy | None, tool: AssistantToolPolicy | None
) -> bool:
    """Whether policy lets anyone use this tool, before looking at the person.

    A missing module policy — the seed not yet run — means reads on and writes
    off. A write that nobody has explicitly enabled must not run. A client
    tool follows the read switch: pressing what is on the screen is bounded by
    the screen, not by the module's write policy, and the route behind the
    button still decides.
    """
    if tool is not None and not tool.enabled:
        return False
    if spec.is_write:
        return module is not None and module.write_enabled
    return module is None or module.read_enabled


def may_delete(actor: Actor) -> bool:
    """Whether this person may have the assistant delete anything at all.

    Managers and above — ``DELETE_ROLES`` — and nobody else, whatever the
    module and tool policies say. A member or a team lead asking the assistant
    to remove something is told it is a manager's call. The same answer is
    handed to the browser, which refuses to press a delete button on their
    behalf: two enforcement points, one rule, read from one list.
    """
    return not actor.roles.isdisjoint(DELETE_ROLES)


def resolve_tools(
    settings: AssistantSettings,
    module_policies: dict[str, AssistantModulePolicy],
    tool_policies: dict[str, AssistantToolPolicy],
    actor: Actor,
) -> list[ResolvedTool]:
    """The tools this person is shown, in catalogue order.

    Four gates, and the last two are different questions about the same person.
    ``allowed_roles`` decides whether they see the module; ``write_roles``
    decides whether the writes in it are offered to them. Somebody who passes
    the first and not the second gets the module's reads and none of its
    writes, which is the ordinary shape of "everyone can look, managers can
    change".

    Planned tools are not in ``LIVE_TOOLS`` and so cannot appear here
    however the policy rows are set — which is the point of the
    distinction rather than a side effect of it.

    A fifth gate sits under all of those and cannot be opened from the
    settings screen: a tool that deletes is offered only to ``DELETE_ROLES``.
    ``write_roles`` may narrow that further; nothing widens it.
    """
    out: list[ResolvedTool] = []
    deleter = may_delete(actor)
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
        if spec.is_write:
            writers = effective_write_roles(module, tool)
            if writers and actor.roles.isdisjoint(writers):
                continue
            if spec.is_destructive and not deleter:
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


def _usd(total: Decimal) -> Decimal:
    return (total / _MILLION).quantize(Decimal("0.000001"))


def speech_cost_of(model: AssistantVoiceModel, *, characters: int) -> Decimal:
    """USD for reading one piece of text aloud.

    Characters, not tokens, because characters are what we can count: the audio
    streams straight through to the browser and is never measured on this side,
    and OpenAI reports nothing back on a speech call. Counting the input we were
    about to send is therefore both the simplest answer and the only exact one.
    """
    return _usd(Decimal(max(characters, 0)) * model.char_price)


def realtime_cost_of(
    model: AssistantVoiceModel,
    *,
    text_input_tokens: int = 0,
    cached_text_input_tokens: int = 0,
    audio_input_tokens: int = 0,
    cached_audio_input_tokens: int = 0,
    text_output_tokens: int = 0,
    audio_output_tokens: int = 0,
) -> Decimal:
    """USD for one spoken conversation, from the usage OpenAI reported to the browser.

    Audio and text are priced apart because they differ by an order of
    magnitude: averaging them into one rate would overstate a session that was
    mostly text and understate every session that was actually spoken, which is
    all of them. The cached counts arrive already included in their matching
    input figure, so they are taken out before the full price applies — the
    same convention as ``cost_of``, and worth keeping identical so that nobody
    reading one has to check the other.
    """
    plain_text = max(text_input_tokens - cached_text_input_tokens, 0)
    plain_audio = max(audio_input_tokens - cached_audio_input_tokens, 0)
    total = (
        Decimal(plain_text) * model.text_input_price
        + Decimal(max(cached_text_input_tokens, 0)) * model.cached_text_input_price
        + Decimal(plain_audio) * model.audio_input_price
        + Decimal(max(cached_audio_input_tokens, 0)) * model.cached_audio_input_price
        + Decimal(max(text_output_tokens, 0)) * model.text_output_price
        + Decimal(max(audio_output_tokens, 0)) * model.audio_output_price
    )
    return _usd(total)
