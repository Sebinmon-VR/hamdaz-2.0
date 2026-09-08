"""The assistant's configuration and record, as database operations.

The rules that matter live here rather than in the router, because they must
hold however a change arrives — HTTP, the seeder, or a future CLI:

* the active model must exist and be enabled; it cannot be disabled while active
* a policy row is only ever *narrowed* from the code catalogue, never invented
* an access rule names a real user, team or role, resolved when it is made
* a run's events are numbered in order and never rewritten
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Date, cast, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.access.service import effective_access
from app.assistant.catalogue import (
    GROUPS,
    GROUPS_BY_KEY,
    LIVE_TOOLS,
    MODELS,
    REALTIME_MODELS,
    REASONING_EFFORTS,
    SPEECH_MODELS,
    TOOLS,
    TOOLS_BY_KEY,
    VOICES,
)
from app.assistant.policy import (
    Actor,
    Admission,
    admit,
    effective_confirm,
    effective_roles,
    tool_enabled,
)
from app.models.assistant import (
    AssistantAccessRule,
    AssistantConversation,
    AssistantMessage,
    AssistantModel,
    AssistantModulePolicy,
    AssistantRun,
    AssistantRunEvent,
    AssistantSettings,
    AssistantToolPolicy,
    AudienceMode,
    EventKind,
    RuleEffect,
    RunStatus,
    SubjectType,
)
from app.models.role import Role
from app.models.team import Team, TeamMembership
from app.models.user import User
from app.roles.service import global_role_keys
from app.teams import service as teams_service
from app.teams.service import TeamNotFoundError


class AssistantError(Exception):
    """An operation was refused. The message is safe to show a person."""


class AssistantNotFoundError(AssistantError):
    pass


class AssistantConflictError(AssistantError):
    pass


class AssistantRefusedError(AssistantError):
    """The person may not use the assistant right now. Carries the admission."""

    def __init__(self, admission: Admission) -> None:
        super().__init__(admission.reason or "Not available")
        self.admission = admission


# ── settings ───────────────────────────────────────────────────────────


async def get_settings(session: AsyncSession) -> AssistantSettings:
    """The single settings row, created with defaults on first use."""
    settings = await session.get(AssistantSettings, 1)
    if settings is None:
        settings = AssistantSettings(id=1)
        session.add(settings)
        await session.flush()
    return settings


async def update_settings(
    session: AsyncSession, *, actor_id: uuid.UUID | None, changes: dict[str, Any]
) -> AssistantSettings:
    """Apply ``changes`` — only the keys present. A cap set to None is removed."""
    settings = await get_settings(session)

    if "model_key" in changes and changes["model_key"] is not None:
        model = await session.get(AssistantModel, changes["model_key"])
        if model is None:
            raise AssistantNotFoundError(f"No model named {changes['model_key']!r}")
        if not model.enabled:
            raise AssistantConflictError(f"{model.name} is disabled; enable it first")
        settings.model_key = model.key
    if (effort := changes.get("reasoning_effort")) is not None:
        if effort not in REASONING_EFFORTS:
            raise AssistantError(
                f"reasoning_effort must be one of: {', '.join(REASONING_EFFORTS)}"
            )
        settings.reasoning_effort = effort
    if (speech := changes.get("voice_model")) is not None:
        if speech not in SPEECH_MODELS:
            raise AssistantError(
                f"voice_model must be one of: {', '.join(SPEECH_MODELS)}"
            )
        settings.voice_model = speech
    if (realtime := changes.get("realtime_model")) is not None:
        if realtime not in REALTIME_MODELS:
            raise AssistantError(
                f"realtime_model must be one of: {', '.join(REALTIME_MODELS)}"
            )
        settings.realtime_model = realtime
    if (voice := changes.get("voice")) is not None:
        if voice not in VOICES:
            raise AssistantError(f"voice must be one of: {', '.join(VOICES)}")
        settings.voice = voice
    if (mode := changes.get("audience_mode")) is not None:
        if mode not in (AudienceMode.EVERYONE, AudienceMode.ALLOW_LIST):
            raise AssistantError("audience_mode must be 'everyone' or 'allow_list'")
        settings.audience_mode = mode

    for field in (
        "enabled",
        "max_tool_rounds",
        "max_output_tokens",
        "history_window",
        "turns_per_user_per_hour",
        "confirm_writes_default",
        "voice_enabled",
        "realtime_enabled",
        "realtime_writes_enabled",
    ):
        if changes.get(field) is not None:
            setattr(settings, field, changes[field])
    # Nullable fields: presence means "set", even to None.
    for field in (
        "daily_cost_cap_user_usd",
        "daily_cost_cap_total_usd",
        "extra_instructions",
        "voice_instructions",
    ):
        if field in changes:
            value = changes[field]
            if isinstance(value, str) and not value.strip():
                value = None
            setattr(settings, field, value)

    settings.updated_by_id = actor_id
    await session.flush()
    return settings


# ── models ─────────────────────────────────────────────────────────────


async def seed_models(session: AsyncSession) -> list[AssistantModel]:
    """Bring the models table in line with the catalogue.

    Prices are written only when a model is first seen: a super admin may have
    corrected them since, and a deploy must not undo that. Names and
    descriptions follow the code.
    """
    existing = {m.key: m for m in (await session.scalars(select(AssistantModel))).all()}
    seeded: list[AssistantModel] = []
    for order, spec in enumerate(MODELS):
        model = existing.get(spec.key)
        if model is None:
            model = AssistantModel(
                key=spec.key,
                name=spec.name,
                description=spec.description,
                input_price=spec.input_price,
                cached_input_price=spec.cached_input_price,
                output_price=spec.output_price,
            )
            session.add(model)
        model.name = spec.name
        model.description = spec.description
        model.sort_order = order
        seeded.append(model)
    await session.flush()
    return seeded


async def list_models(session: AsyncSession) -> list[AssistantModel]:
    return list(
        (await session.scalars(select(AssistantModel).order_by(AssistantModel.sort_order))).all()
    )


async def get_model(session: AsyncSession, key: str) -> AssistantModel:
    model = await session.get(AssistantModel, key)
    if model is None:
        raise AssistantNotFoundError(f"No model named {key!r}")
    return model


async def update_model(
    session: AsyncSession, key: str, *, actor_id: uuid.UUID | None, changes: dict[str, Any]
) -> AssistantModel:
    model = await get_model(session, key)
    if changes.get("enabled") is False:
        settings = await get_settings(session)
        if settings.model_key == key:
            raise AssistantConflictError(
                f"{model.name} is the active model; pick another in settings first"
            )
    for field in ("enabled", "input_price", "cached_input_price", "output_price"):
        if changes.get(field) is not None:
            setattr(model, field, changes[field])
    model.updated_by_id = actor_id
    await session.flush()
    return model


# ── policies ───────────────────────────────────────────────────────────


async def seed_policies(session: AsyncSession) -> tuple[int, int]:
    """Create a policy row for every module and tool that lacks one.

    Existing rows are left exactly as the super admin set them. A tool that has
    gone from the catalogue loses its row — there is nothing left to switch.
    Returns (modules, tools) present afterwards.
    """
    modules = {
        p.module_key: p for p in (await session.scalars(select(AssistantModulePolicy))).all()
    }
    for group in GROUPS:
        if group.key not in modules:
            session.add(AssistantModulePolicy(module_key=group.key))

    tools = {p.tool_key: p for p in (await session.scalars(select(AssistantToolPolicy))).all()}
    # Only live tools get a policy row. A planned one has nothing to
    # switch, and giving it a row would imply it could be turned on.
    for spec in LIVE_TOOLS:
        policy = tools.get(spec.key)
        if policy is None:
            session.add(AssistantToolPolicy(tool_key=spec.key, module_key=spec.module_key))
        elif policy.module_key != spec.module_key:
            policy.module_key = spec.module_key
    live_keys = {spec.key for spec in LIVE_TOOLS}
    stale = [key for key in tools if key not in live_keys]
    if stale:
        await session.execute(
            delete(AssistantToolPolicy).where(AssistantToolPolicy.tool_key.in_(stale))
        )
    await session.flush()
    return len(GROUPS), len(LIVE_TOOLS)


async def module_policies(session: AsyncSession) -> dict[str, AssistantModulePolicy]:
    rows = (await session.scalars(select(AssistantModulePolicy))).all()
    return {p.module_key: p for p in rows}


async def tool_policies(session: AsyncSession) -> dict[str, AssistantToolPolicy]:
    rows = (await session.scalars(select(AssistantToolPolicy))).all()
    return {p.tool_key: p for p in rows}


def _check_roles(roles: list[str] | None) -> list[str] | None:
    if roles is None:
        return None
    cleaned = [r.strip() for r in roles if r and r.strip()]
    return cleaned or None


async def update_module_policy(
    session: AsyncSession, module_key: str, *, actor_id: uuid.UUID | None, changes: dict[str, Any]
) -> AssistantModulePolicy:
    """``changes`` holds only the keys the caller set; None clears a nullable field."""
    if module_key not in GROUPS_BY_KEY:
        raise AssistantNotFoundError(f"The assistant has no module named {module_key!r}")
    policy = await session.get(AssistantModulePolicy, module_key)
    if policy is None:
        policy = AssistantModulePolicy(module_key=module_key)
        session.add(policy)
    for field in ("read_enabled", "write_enabled"):
        if changes.get(field) is not None:
            setattr(policy, field, changes[field])
    if "confirm_writes" in changes:
        policy.confirm_writes = changes["confirm_writes"]
    if "allowed_roles" in changes:
        policy.allowed_roles = _check_roles(changes["allowed_roles"])
    policy.updated_by_id = actor_id
    await session.flush()
    return policy


async def update_tool_policy(
    session: AsyncSession, tool_key: str, *, actor_id: uuid.UUID | None, changes: dict[str, Any]
) -> AssistantToolPolicy:
    spec = TOOLS_BY_KEY.get(tool_key)
    if spec is None:
        raise AssistantNotFoundError(f"The assistant has no tool named {tool_key!r}")
    policy = await session.get(AssistantToolPolicy, tool_key)
    if policy is None:
        policy = AssistantToolPolicy(tool_key=tool_key, module_key=spec.module_key)
        session.add(policy)
    if changes.get("enabled") is not None:
        policy.enabled = changes["enabled"]
    if "confirm_override" in changes:
        policy.confirm_override = changes["confirm_override"]
    if "allowed_roles" in changes:
        policy.allowed_roles = _check_roles(changes["allowed_roles"])
    policy.updated_by_id = actor_id
    await session.flush()
    return policy


def policy_matrix(
    settings: AssistantSettings,
    modules: dict[str, AssistantModulePolicy],
    tools: dict[str, AssistantToolPolicy],
) -> list[dict[str, Any]]:
    """Every module with its tools and the flags that actually apply, for the admin screen."""
    out: list[dict[str, Any]] = []
    for group in GROUPS:
        module = modules.get(group.key)
        rows: list[dict[str, Any]] = []
        # Every tool, including the planned ones. This is the one place
        # that shows the whole picture, because it is the one caller who
        # should see what is coming as well as what is here.
        for spec in TOOLS:
            if spec.module_key != group.key:
                continue
            tool = tools.get(spec.key)
            rows.append(
                {
                    "tool_key": spec.key,
                    "label": spec.label,
                    "kind": spec.kind,
                    "method": spec.method,
                    "path": spec.path,
                    "description": spec.description,
                    "warning": spec.warning,
                    "status": spec.status,
                    "deferred": spec.deferred,
                    "enabled": tool.enabled if tool else True,
                    "confirm_override": tool.confirm_override if tool else None,
                    "allowed_roles": (
                        list(tool.allowed_roles) if tool and tool.allowed_roles else None
                    ),
                    "effective_enabled": spec.is_live and tool_enabled(spec, module, tool),
                    "effective_confirm": (
                        spec.is_write and effective_confirm(settings, module, tool)
                    ),
                    "effective_roles": effective_roles(module, tool),
                }
            )
        out.append(
            {
                "module_key": group.key,
                "name": group.name,
                "gate": group.gate,
                "description": group.description,
                "read_enabled": module.read_enabled if module else True,
                "write_enabled": module.write_enabled if module else False,
                "confirm_writes": module.confirm_writes if module else None,
                "allowed_roles": (
                    list(module.allowed_roles) if module and module.allowed_roles else None
                ),
                "effective_confirm": effective_confirm(settings, module, None),
                "tools": rows,
            }
        )
    return out


# ── access rules ───────────────────────────────────────────────────────


async def list_rules(session: AsyncSession) -> list[AssistantAccessRule]:
    return list(
        (
            await session.scalars(
                select(AssistantAccessRule).order_by(AssistantAccessRule.created_at)
            )
        ).all()
    )


async def _resolve_subject(
    session: AsyncSession, subject_type: str, subject: str
) -> tuple[str, str]:
    """(subject_id, label) for a rule, or AssistantNotFoundError."""
    subject = subject.strip()
    if subject_type == SubjectType.USER:
        try:
            match = User.id == uuid.UUID(subject)
        except ValueError:
            # Not a UUID, so it is an email or an Entra object id.
            match = (User.email == subject.lower()) | (User.entra_object_id == subject)
        user = await session.scalar(select(User).where(match))
        if user is None:
            raise AssistantNotFoundError(
                f"No user matches {subject!r}. They must have signed in at least once."
            )
        return str(user.id), f"{user.display_name} <{user.email}>"
    if subject_type == SubjectType.TEAM:
        try:
            team = await teams_service.get_team(session, subject)
        except TeamNotFoundError as exc:
            raise AssistantNotFoundError(str(exc)) from exc
        return str(team.id), f"{team.name} ({team.slug})"
    if subject_type == SubjectType.ROLE:
        role = await session.scalar(select(Role).where(Role.key == subject))
        if role is None:
            raise AssistantNotFoundError(f"No role named {subject!r}")
        return role.key, role.name
    raise AssistantError("subject_type must be user, team or role")


async def create_rule(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID | None,
    subject_type: str,
    subject: str,
    effect: str,
    enabled: bool = True,
    note: str | None = None,
) -> AssistantAccessRule:
    if effect not in (RuleEffect.ALLOW, RuleEffect.BLOCK):
        raise AssistantError("effect must be allow or block")
    subject_id, label = await _resolve_subject(session, subject_type, subject)
    duplicate = await session.scalar(
        select(AssistantAccessRule).where(
            AssistantAccessRule.subject_type == subject_type,
            AssistantAccessRule.subject_id == subject_id,
            AssistantAccessRule.effect == effect,
        )
    )
    if duplicate is not None:
        raise AssistantConflictError(f"A rule already {effect}s {label}")
    rule = AssistantAccessRule(
        subject_type=subject_type,
        subject_id=subject_id,
        subject_label=label,
        effect=effect,
        enabled=enabled,
        note=note,
        created_by_id=actor_id,
    )
    session.add(rule)
    await session.flush()
    return rule


async def get_rule(session: AsyncSession, rule_id: uuid.UUID) -> AssistantAccessRule:
    rule = await session.get(AssistantAccessRule, rule_id)
    if rule is None:
        raise AssistantNotFoundError("No such access rule")
    return rule


async def update_rule(
    session: AsyncSession, rule_id: uuid.UUID, *, changes: dict[str, Any]
) -> AssistantAccessRule:
    rule = await get_rule(session, rule_id)
    if changes.get("effect") is not None:
        if changes["effect"] not in (RuleEffect.ALLOW, RuleEffect.BLOCK):
            raise AssistantError("effect must be allow or block")
        rule.effect = changes["effect"]
    if changes.get("enabled") is not None:
        rule.enabled = changes["enabled"]
    if "note" in changes:
        rule.note = changes["note"]
    await session.flush()
    return rule


async def delete_rule(session: AsyncSession, rule_id: uuid.UUID) -> None:
    rule = await get_rule(session, rule_id)
    await session.delete(rule)
    await session.flush()


# ── the caller, and what applies to them ───────────────────────────────


@dataclass(slots=True)
class Snapshot:
    """Everything the policy needs, loaded once so a turn sees one consistent picture."""

    settings: AssistantSettings
    model: AssistantModel | None
    module_policies: dict[str, AssistantModulePolicy]
    tool_policies: dict[str, AssistantToolPolicy]
    rules: list[AssistantAccessRule]


async def load_snapshot(session: AsyncSession) -> Snapshot:
    settings = await get_settings(session)
    return Snapshot(
        settings=settings,
        model=await session.get(AssistantModel, settings.model_key),
        module_policies=await module_policies(session),
        tool_policies=await tool_policies(session),
        rules=[r for r in await list_rules(session) if r.enabled],
    )


async def build_actor(session: AsyncSession, user: User) -> Actor:
    roles = await global_role_keys(session, user.id)
    memberships = await teams_service.teams_for_user(session, user.id)
    access = await effective_access(session, user_id=user.id, global_roles=roles)
    return Actor(
        user_id=user.id,
        roles=frozenset(roles),
        team_ids=frozenset(team.id for team, _ in memberships),
        access_modules=frozenset(m["key"] for m in access["modules"]),
    )


def _today_start() -> datetime:
    now = datetime.now(UTC)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


async def check_limits(
    session: AsyncSession, settings: AssistantSettings, user_id: uuid.UUID
) -> Admission:
    """The per-person rate limit and the daily cost caps. Blocked runs do not count."""
    counted = AssistantRun.status != RunStatus.BLOCKED
    hour_ago = datetime.now(UTC) - timedelta(hours=1)
    turns = await session.scalar(
        select(func.count()).where(
            AssistantRun.user_id == user_id, AssistantRun.started_at >= hour_ago, counted
        )
    )
    if turns is not None and turns >= settings.turns_per_user_per_hour:
        return Admission(
            False, "rate_limited", "You have sent a lot of messages in the last hour. Try later."
        )
    if settings.daily_cost_cap_user_usd is not None:
        spent = await session.scalar(
            select(func.coalesce(func.sum(AssistantRun.cost_usd), 0)).where(
                AssistantRun.user_id == user_id, AssistantRun.started_at >= _today_start()
            )
        )
        if Decimal(spent or 0) >= settings.daily_cost_cap_user_usd:
            return Admission(
                False, "cost_cap_user", "You have reached today's usage limit for the assistant."
            )
    if settings.daily_cost_cap_total_usd is not None:
        spent = await session.scalar(
            select(func.coalesce(func.sum(AssistantRun.cost_usd), 0)).where(
                AssistantRun.started_at >= _today_start()
            )
        )
        if Decimal(spent or 0) >= settings.daily_cost_cap_total_usd:
            return Admission(
                False, "cost_cap_total", "The assistant has reached today's usage limit."
            )
    return Admission(True)


async def admission_for(
    session: AsyncSession, snapshot: Snapshot, actor: Actor
) -> Admission:
    """Every gate in order: switch, rules, audience, then limits."""
    verdict = admit(snapshot.settings, snapshot.rules, actor)
    if not verdict.admitted:
        return verdict
    return await check_limits(session, snapshot.settings, actor.user_id)


# ── conversations ──────────────────────────────────────────────────────


async def create_conversation(
    session: AsyncSession, *, user: User, title: str | None = None
) -> AssistantConversation:
    conversation = AssistantConversation(user_id=user.id, title=title)
    session.add(conversation)
    await session.flush()
    return conversation


async def list_conversations(
    session: AsyncSession, user_id: uuid.UUID, *, limit: int = 50
) -> list[AssistantConversation]:
    return list(
        (
            await session.scalars(
                select(AssistantConversation)
                .where(
                    AssistantConversation.user_id == user_id,
                    AssistantConversation.archived_at.is_(None),
                )
                .order_by(
                    func.coalesce(
                        AssistantConversation.last_message_at, AssistantConversation.created_at
                    ).desc()
                )
                .limit(limit)
            )
        ).all()
    )


async def get_conversation(
    session: AsyncSession, conversation_id: uuid.UUID, *, user_id: uuid.UUID | None = None
) -> AssistantConversation:
    """One conversation. With ``user_id`` it must be theirs — a 404 either way,
    because whether somebody else's conversation exists is not theirs to learn."""
    conversation = await session.get(AssistantConversation, conversation_id)
    if conversation is None or (user_id is not None and conversation.user_id != user_id):
        raise AssistantNotFoundError("No such conversation")
    return conversation


async def delete_conversation(session: AsyncSession, conversation: AssistantConversation) -> None:
    await session.delete(conversation)
    await session.flush()


async def list_messages(
    session: AsyncSession, conversation_id: uuid.UUID
) -> list[AssistantMessage]:
    return list(
        (
            await session.scalars(
                select(AssistantMessage)
                .where(AssistantMessage.conversation_id == conversation_id)
                .order_by(AssistantMessage.seq)
            )
        ).all()
    )


async def add_message(
    session: AsyncSession,
    conversation: AssistantConversation,
    *,
    role: str,
    content: str,
    run_id: uuid.UUID | None,
    tool_calls: list[dict[str, Any]] | None = None,
) -> AssistantMessage:
    last = await session.scalar(
        select(func.max(AssistantMessage.seq)).where(
            AssistantMessage.conversation_id == conversation.id
        )
    )
    message = AssistantMessage(
        conversation_id=conversation.id,
        run_id=run_id,
        seq=(last or 0) + 1,
        role=role,
        content=content,
        tool_calls=tool_calls,
    )
    session.add(message)
    conversation.last_message_at = datetime.now(UTC)
    if conversation.title is None and role == "user":
        conversation.title = content.strip().splitlines()[0][:80] if content.strip() else None
    await session.flush()
    return message


async def open_run_for(
    session: AsyncSession, conversation_id: uuid.UUID
) -> AssistantRun | None:
    """The run still in flight on this conversation, if any. At most one may be."""
    return await session.scalar(
        select(AssistantRun)
        .where(
            AssistantRun.conversation_id == conversation_id,
            AssistantRun.status.in_([RunStatus.RUNNING, RunStatus.AWAITING_CONFIRMATION]),
        )
        .order_by(AssistantRun.started_at.desc())
    )


# ── runs ───────────────────────────────────────────────────────────────


async def create_run(
    session: AsyncSession,
    *,
    conversation: AssistantConversation,
    user: User,
    settings: AssistantSettings,
    user_text: str,
    status: str = RunStatus.RUNNING,
) -> AssistantRun:
    run = AssistantRun(
        conversation_id=conversation.id,
        user_id=user.id,
        status=status,
        model_key=settings.model_key,
        reasoning_effort=settings.reasoning_effort,
        started_at=datetime.now(UTC),
        user_text=user_text,
        transcript=[],
    )
    session.add(run)
    await session.flush()
    return run


async def get_run(
    session: AsyncSession, run_id: uuid.UUID, *, with_events: bool = False
) -> AssistantRun:
    query = select(AssistantRun).where(AssistantRun.id == run_id)
    if with_events:
        query = query.options(selectinload(AssistantRun.events))
    run = await session.scalar(query)
    if run is None:
        raise AssistantNotFoundError("No such run")
    return run


async def add_event(
    session: AsyncSession,
    run: AssistantRun,
    kind: str,
    *,
    tool_key: str | None = None,
    payload: dict[str, Any] | None = None,
) -> AssistantRunEvent:
    last = await session.scalar(
        select(func.max(AssistantRunEvent.seq)).where(AssistantRunEvent.run_id == run.id)
    )
    event = AssistantRunEvent(
        run_id=run.id, seq=(last or 0) + 1, kind=kind, tool_key=tool_key, payload=payload
    )
    session.add(event)
    await session.flush()
    return event


async def list_runs(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | None = None,
    team_id: uuid.UUID | None = None,
    status: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[AssistantRun], int]:
    query = select(AssistantRun)
    if user_id is not None:
        query = query.where(AssistantRun.user_id == user_id)
    if team_id is not None:
        members = select(TeamMembership.user_id).where(TeamMembership.team_id == team_id)
        query = query.where(AssistantRun.user_id.in_(members))
    if status:
        query = query.where(AssistantRun.status == status)
    if since is not None:
        query = query.where(AssistantRun.started_at >= since)
    if until is not None:
        query = query.where(AssistantRun.started_at < until)
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    rows = await session.scalars(
        query.order_by(AssistantRun.started_at.desc()).limit(limit).offset(offset)
    )
    return list(rows.all()), int(total or 0)


async def open_runs(session: AsyncSession) -> list[AssistantRun]:
    return list(
        (
            await session.scalars(
                select(AssistantRun)
                .where(
                    AssistantRun.status.in_([RunStatus.RUNNING, RunStatus.AWAITING_CONFIRMATION])
                )
                .order_by(AssistantRun.started_at.desc())
            )
        ).all()
    )


async def request_cancel(session: AsyncSession, run: AssistantRun) -> AssistantRun:
    """Ask a running turn to stop. A paused one stops at once — nothing is in flight."""
    if not run.is_open:
        raise AssistantConflictError(f"That run already finished ({run.status})")
    run.cancel_requested = True
    if run.status == RunStatus.AWAITING_CONFIRMATION:
        run.status = RunStatus.CANCELLED
        run.pending = None
        run.finished_at = datetime.now(UTC)
        await add_event(session, run, EventKind.CANCELLED, payload={"by": "super_admin"})
    await session.flush()
    return run


# ── analytics ──────────────────────────────────────────────────────────


def _bucket(key: str, label: str, row: Any) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "runs": int(row.runs or 0),
        "tool_calls": int(row.tool_calls or 0),
        "input_tokens": int(row.input_tokens or 0),
        "output_tokens": int(row.output_tokens or 0),
        "cost_usd": Decimal(row.cost_usd or 0),
    }


async def analytics(session: AsyncSession, *, since: date, until: date) -> dict[str, Any]:
    """Totals and breakdowns over ``[since, until]`` inclusive, by day."""
    start = datetime.combine(since, datetime.min.time(), tzinfo=UTC)
    end = datetime.combine(until + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    in_range = (AssistantRun.started_at >= start) & (AssistantRun.started_at < end)

    sums = (
        func.count().label("runs"),
        func.coalesce(func.sum(AssistantRun.tool_calls), 0).label("tool_calls"),
        func.coalesce(func.sum(AssistantRun.input_tokens), 0).label("input_tokens"),
        func.coalesce(func.sum(AssistantRun.output_tokens), 0).label("output_tokens"),
        func.coalesce(func.sum(AssistantRun.cost_usd), 0).label("cost_usd"),
    )

    totals_row = (
        await session.execute(
            select(
                *sums,
                func.coalesce(func.sum(AssistantRun.cached_input_tokens), 0).label("cached"),
                func.coalesce(func.sum(AssistantRun.reasoning_tokens), 0).label("reasoning"),
                func.count(func.distinct(AssistantRun.user_id)).label("people"),
            ).where(in_range)
        )
    ).one()

    by_status = {
        row.status: int(row.n)
        for row in (
            await session.execute(
                select(AssistantRun.status, func.count().label("n"))
                .where(in_range)
                .group_by(AssistantRun.status)
            )
        ).all()
    }

    event_counts = {
        row.kind: int(row.n)
        for row in (
            await session.execute(
                select(AssistantRunEvent.kind, func.count().label("n"))
                .join(AssistantRun, AssistantRun.id == AssistantRunEvent.run_id)
                .where(in_range)
                .group_by(AssistantRunEvent.kind)
            )
        ).all()
    }

    by_day = [
        _bucket(row.day.isoformat(), row.day.isoformat(), row)
        for row in (
            await session.execute(
                select(cast(AssistantRun.started_at, Date).label("day"), *sums)
                .where(in_range)
                .group_by("day")
                .order_by("day")
            )
        ).all()
    ]

    by_user = [
        _bucket(str(row.user_id), f"{row.display_name} <{row.email}>", row)
        for row in (
            await session.execute(
                select(AssistantRun.user_id, User.display_name, User.email, *sums)
                .join(User, User.id == AssistantRun.user_id)
                .where(in_range)
                .group_by(AssistantRun.user_id, User.display_name, User.email)
                .order_by(func.sum(AssistantRun.cost_usd).desc())
            )
        ).all()
    ]

    # A person on two teams is counted for both: each team lead sees what their
    # people used. The team figures therefore add up to more than the total,
    # which is correct and worth knowing.
    by_team = [
        _bucket(str(row.team_id), row.name, row)
        for row in (
            await session.execute(
                select(TeamMembership.team_id, Team.name, *sums)
                .select_from(AssistantRun)
                .join(TeamMembership, TeamMembership.user_id == AssistantRun.user_id)
                .join(Team, Team.id == TeamMembership.team_id)
                .where(in_range)
                .group_by(TeamMembership.team_id, Team.name)
                .order_by(func.sum(AssistantRun.cost_usd).desc())
            )
        ).all()
    ]

    by_model = [
        _bucket(row.model_key, row.model_key, row)
        for row in (
            await session.execute(
                select(AssistantRun.model_key, *sums)
                .where(in_range)
                .group_by(AssistantRun.model_key)
                .order_by(func.sum(AssistantRun.cost_usd).desc())
            )
        ).all()
    ]

    by_tool = []
    for row in (
        await session.execute(
            select(AssistantRunEvent.tool_key, func.count().label("n"))
            .join(AssistantRun, AssistantRun.id == AssistantRunEvent.run_id)
            .where(in_range, AssistantRunEvent.kind == EventKind.TOOL_CALL)
            .group_by(AssistantRunEvent.tool_key)
            .order_by(func.count().desc())
        )
    ).all():
        spec = TOOLS_BY_KEY.get(row.tool_key or "")
        by_tool.append(
            {
                "key": row.tool_key,
                "label": spec.label if spec else (row.tool_key or "?"),
                "runs": 0,
                "tool_calls": int(row.n),
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_usd": Decimal(0),
            }
        )

    return {
        "since": since,
        "until": until,
        "totals": {
            "runs": int(totals_row.runs or 0),
            "completed": by_status.get(RunStatus.COMPLETED, 0),
            "failed": by_status.get(RunStatus.FAILED, 0),
            "blocked": by_status.get(RunStatus.BLOCKED, 0),
            "cancelled": by_status.get(RunStatus.CANCELLED, 0),
            "open": by_status.get(RunStatus.RUNNING, 0)
            + by_status.get(RunStatus.AWAITING_CONFIRMATION, 0),
            "people": int(totals_row.people or 0),
            "tool_calls": int(totals_row.tool_calls or 0),
            "input_tokens": int(totals_row.input_tokens or 0),
            "cached_input_tokens": int(totals_row.cached or 0),
            "output_tokens": int(totals_row.output_tokens or 0),
            "reasoning_tokens": int(totals_row.reasoning or 0),
            "cost_usd": Decimal(totals_row.cost_usd or 0),
            "confirmations_requested": event_counts.get(EventKind.CONFIRMATION_REQUESTED, 0),
            "confirmations_approved": event_counts.get(EventKind.CONFIRMED, 0),
            "confirmations_declined": event_counts.get(EventKind.DECLINED, 0),
            "refused_by_policy": event_counts.get(EventKind.BLOCKED_BY_POLICY, 0),
        },
        "by_day": by_day,
        "by_user": by_user,
        "by_team": by_team,
        "by_model": by_model,
        "by_tool": by_tool,
    }
