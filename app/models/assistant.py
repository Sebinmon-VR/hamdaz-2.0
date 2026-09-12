"""The assistant: what the super admin has configured, and what it has done.

Two halves, kept in one file because they are read together on every turn.

**Configuration** — settings, the model list, the per-module and per-tool
policies, and the access rules. All of it is data rather than code so a super
admin can change a rule at runtime without a deploy. The seeder fills the
policy tables from the code catalogue; the super admin edits what it seeded.

**Record** — conversations, the messages a person sees, and one *run* per turn
with an ordered event log. The run is the unit of audit and of cost: every
refusal, every tool call, every confirmation and every token is recorded
against one, which is what makes "what did the assistant do for whom, and what
did it cost" answerable later.

Policies here can only ever *narrow* what a person may do. A tool call still
goes through the real route with the person's own session, so enabling a write
for the assistant never grants anyone a right they did not already hold.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Timestamped, UUIDPrimaryKey
from app.models.user import User

# ── configuration ──────────────────────────────────────────────────────


class AudienceMode(StrEnum):
    #: Everyone signed in, minus anyone a block rule names.
    EVERYONE = "everyone"
    #: Nobody unless an allow rule names them. The release-gradually mode.
    ALLOW_LIST = "allow_list"


class AssistantSettings(Base, Timestamped):
    """The super admin's global switches. One row; ``id`` is fixed at 1."""

    __tablename__ = "assistant_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)

    #: The master switch. Off by default: an assistant nobody has configured
    #: should not be reachable by anyone, super admins included.
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    #: Which of ``assistant_models`` answers. Must be an enabled model.
    #: Measured against this app's own tool payload, not chosen by tier. Terra
    #: reached the first word in about 2.3s where Sol took 3.4s, and did it more
    #: consistently — and a chat assistant is judged on the wait far more than on
    #: the last few points of reasoning. Sol and Astra remain one setting away.
    model_key: Mapped[str] = mapped_column(
        String(64), default="gpt-5.6-terra", server_default=text("'gpt-5.6-terra'"),
        nullable=False,
    )
    #: ``none`` rather than ``low``: on a question needing no tool at all, low
    #: effort still cost roughly a second and a half of thinking before the first
    #: word. Raise it if tool choice starts going wrong; that is the trade.
    reasoning_effort: Mapped[str] = mapped_column(
        String(16), default="none", server_default=text("'none'"), nullable=False
    )
    #: How many times one turn may go back to the model after tool results.
    max_tool_rounds: Mapped[int] = mapped_column(
        Integer, default=8, server_default=text("8"), nullable=False
    )
    max_output_tokens: Mapped[int] = mapped_column(
        Integer, default=4000, server_default=text("4000"), nullable=False
    )
    #: Earlier messages carried into each new turn, as context.
    history_window: Mapped[int] = mapped_column(
        Integer, default=30, server_default=text("30"), nullable=False
    )
    turns_per_user_per_hour: Mapped[int] = mapped_column(
        Integer, default=60, server_default=text("60"), nullable=False
    )
    #: Null means no cap. Checked before a turn starts, against runs today (UTC).
    daily_cost_cap_user_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    daily_cost_cap_total_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    audience_mode: Mapped[str] = mapped_column(
        String(16),
        default=AudienceMode.ALLOW_LIST,
        server_default=text("'allow_list'"),
        nullable=False,
    )
    #: What a module policy inherits when it does not say.
    confirm_writes_default: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    voice_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    #: The speech model that reads answers aloud. Only ``gpt-4o-mini-tts``
    #: honours ``voice_instructions``; the ``tts-1`` pair ignore it.
    voice_model: Mapped[str] = mapped_column(
        String(40), default="gpt-4o-mini-tts", server_default=text("'gpt-4o-mini-tts'"),
        nullable=False,
    )
    #: Which of OpenAI's voices speaks. See ``catalogue.VOICES``.
    voice: Mapped[str] = mapped_column(
        String(24), default="cedar", server_default=text("'cedar'"), nullable=False
    )
    #: Spoken conversation, where OpenAI runs the loop and the browser streams
    #: audio to it directly. Separate from ``voice_enabled``, which only reads
    #: written answers aloud through our own turn: this one is a different
    #: architecture with a weaker confirmation guarantee, so turning on one must
    #: not quietly turn on the other.
    realtime_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    realtime_model: Mapped[str] = mapped_column(
        String(40), default="gpt-realtime-2.1", server_default=text("'gpt-realtime-2.1'"),
        nullable=False,
    )
    #: Whether the assistant may perform writes while in a spoken conversation.
    #: Off by default and deliberately its own switch: in voice mode it is the
    #: client that asks "shall I go ahead", not the server, and an administrator
    #: should have to accept that before a spoken word can change data.
    realtime_writes_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    #: How it should sound, in words — pace, warmth, what to slow down for.
    #: Null falls back to the shipped wording in ``catalogue.VOICE_INSTRUCTIONS``,
    #: so an empty box means "the default", not "no steering at all".
    voice_instructions: Mapped[str | None] = mapped_column(Text)
    #: Appended to the system prompt verbatim: house rules, tone, what not to do.
    extra_instructions: Mapped[str | None] = mapped_column(Text)

    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    def __repr__(self) -> str:
        return f"<AssistantSettings enabled={self.enabled} model={self.model_key}>"


class AssistantModel(Base, Timestamped):
    """A model the super admin may pick, with the prices cost is computed from.

    Seeded from the catalogue; prices are editable because OpenAI changes them
    and a stale price makes every cost figure quietly wrong.
    """

    __tablename__ = "assistant_models"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    #: USD per one million tokens.
    input_price: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    cached_input_price: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    output_price: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    def __repr__(self) -> str:
        return f"<AssistantModel {self.key}>"


class VoiceKind(StrEnum):
    #: Reads a written answer aloud. Billed on the characters we send.
    SPEECH = "speech"
    #: A spoken conversation. Billed on tokens, and audio tokens are the dear ones.
    REALTIME = "realtime"


class AssistantVoiceModel(Base, Timestamped):
    """A priced voice model — a speech engine or a spoken-conversation model.

    Kept apart from ``AssistantModel`` rather than folded into it because the
    two are not billed in the same unit. The chat model is tokens in, tokens
    out. Speech is charged per character of the text handed to it, and a
    realtime session is charged per token with audio costing many times what
    text does. One table with one set of price columns could hold both only by
    calling a character a token, and then every figure downstream would be a
    guess wearing a decimal point.

    Prices unused by a row's ``kind`` are zero, and the cost functions in
    ``policy`` never read them.
    """

    __tablename__ = "assistant_voice_models"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    #: ``speech`` or ``realtime``.
    kind: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    #: speech: USD per one million characters of input text.
    char_price: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), default=Decimal(0), server_default=text("0"), nullable=False
    )
    #: realtime: USD per one million tokens, by sort.
    text_input_price: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), default=Decimal(0), server_default=text("0"), nullable=False
    )
    cached_text_input_price: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), default=Decimal(0), server_default=text("0"), nullable=False
    )
    audio_input_price: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), default=Decimal(0), server_default=text("0"), nullable=False
    )
    cached_audio_input_price: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), default=Decimal(0), server_default=text("0"), nullable=False
    )
    text_output_price: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), default=Decimal(0), server_default=text("0"), nullable=False
    )
    audio_output_price: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), default=Decimal(0), server_default=text("0"), nullable=False
    )

    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    def __repr__(self) -> str:
        return f"<AssistantVoiceModel {self.key} {self.kind}>"


class UsageSource(StrEnum):
    #: We counted it ourselves, before the request left. Exact.
    SERVER = "server"
    #: The browser told us what OpenAI reported to it. See the note below.
    CLIENT = "client"


class AssistantVoiceUsage(Base, UUIDPrimaryKey, Timestamped):
    """One billable piece of voice: a clip read aloud, or a spoken session.

    Voice does not fit on a run, and pretending it did is why it went uncosted.
    Reading an answer back is not a turn — it is often asked for twice on the
    same text and has no tools and no model round. A spoken conversation is the
    opposite problem: it is one run, but OpenAI's loop, so the tokens never pass
    through this process at all.

    Hence a table of its own, and hence ``source``. A speech row is counted here
    from the text we were about to send, before the request leaves, and is
    exact. A realtime row is what the browser heard OpenAI report at the end of
    the session, which makes it a report rather than a bill: a session whose tab
    was closed reports nothing, so these figures are a floor, not a ceiling. The
    column exists so that whoever reads the cost screen knows which they are
    looking at instead of having to know this paragraph.
    """

    __tablename__ = "assistant_voice_usage"
    __table_args__ = (
        Index("ix_assistant_voice_usage_user_created", "user_id", "created_at"),
        Index("ix_assistant_voice_usage_kind", "kind"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    #: The spoken conversation this belongs to, when there is one. Null for a
    #: clip read aloud, which is deliberately not part of any run.
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assistant_runs.id", ondelete="SET NULL")
    )
    #: ``speech`` or ``realtime``.
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    model_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    voice: Mapped[str | None] = mapped_column(String(24))
    source: Mapped[str] = mapped_column(
        String(8), default=UsageSource.SERVER, server_default=text("'server'"), nullable=False
    )

    #: speech: characters of text handed to the model.
    characters: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    #: realtime: tokens, by sort. ``cached_*`` are already counted in their
    #: matching input figure and are subtracted before the full price applies,
    #: exactly as the chat model's cached tokens are.
    text_input_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    cached_text_input_tokens: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    audio_input_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    cached_audio_input_tokens: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    text_output_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    audio_output_tokens: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    #: How long the spoken conversation lasted, when the client reports it.
    #: Not what it is billed on — that is the tokens — but the number a person
    #: recognises when they are asked why the bill looks like that.
    seconds: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))

    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), default=Decimal(0), server_default=text("0"), nullable=False
    )

    user: Mapped[User] = relationship(foreign_keys=[user_id], lazy="joined")

    def __repr__(self) -> str:
        return f"<AssistantVoiceUsage {self.kind} {self.model_key} ${self.cost_usd}>"


class AssistantModulePolicy(Base, Timestamped):
    """What the assistant may do within one module, for everyone."""

    __tablename__ = "assistant_module_policies"

    module_key: Mapped[str] = mapped_column(String(40), primary_key=True)
    read_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    #: On. It was off, and off it made the assistant an oracle that could tell
    #: you your leave balance and not book a day of it. What replaced the master
    #: off switch is not nothing: it is ``write_roles`` below, which says *who*
    #: may write here rather than *whether* anybody may, and the confirmation
    #: pause, which still puts the action in front of a person before it runs.
    #: A super admin who wants a module read-only again sets this false.
    write_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    #: Null follows ``AssistantSettings.confirm_writes_default``.
    confirm_writes: Mapped[bool | None] = mapped_column(Boolean)
    #: Global role keys. Null or empty means no extra restriction — the route's
    #: own guard still applies, as it always does.
    allowed_roles: Mapped[list[str] | None] = mapped_column(ARRAY(String(40)))
    #: Global role keys that may have the assistant *write* in this module,
    #: where ``allowed_roles`` governs seeing it at all. Null means no extra
    #: restriction beyond the route's.
    #:
    #: Two columns rather than one because the two questions have different
    #: answers for the same person. An ordinary employee should read the team
    #: list and should not be able to say "delete the Kuwait team" — and if the
    #: only lever were ``allowed_roles``, buying the second would cost the
    #: first. Seeded from the module catalogue's shipped default; a super
    #: admin's edit is never overwritten by a later seed.
    write_roles: Mapped[list[str] | None] = mapped_column(ARRAY(String(40)))
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    def __repr__(self) -> str:
        return f"<AssistantModulePolicy {self.module_key}>"


class AssistantToolPolicy(Base, Timestamped):
    """One tool's switches. Anything left null follows the module."""

    __tablename__ = "assistant_tool_policies"

    tool_key: Mapped[str] = mapped_column(String(80), primary_key=True)
    module_key: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    confirm_override: Mapped[bool | None] = mapped_column(Boolean)
    allowed_roles: Mapped[list[str] | None] = mapped_column(ARRAY(String(40)))
    #: Narrows — or widens — the module's ``write_roles`` for this one tool.
    #: The lever for a module where most writes are everyday work and one is
    #: not: leave is anyone's to request and the rules are not anyone's to
    #: rewrite, and that is one column, not a new module.
    write_roles: Mapped[list[str] | None] = mapped_column(ARRAY(String(40)))
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    def __repr__(self) -> str:
        return f"<AssistantToolPolicy {self.tool_key} enabled={self.enabled}>"


class SubjectType(StrEnum):
    USER = "user"
    TEAM = "team"
    ROLE = "role"


class RuleEffect(StrEnum):
    ALLOW = "allow"
    BLOCK = "block"


class AssistantAccessRule(Base, UUIDPrimaryKey, Timestamped):
    """Release the assistant to, or withhold it from, a user, a team or a role."""

    __tablename__ = "assistant_access_rules"
    __table_args__ = (Index("ix_assistant_rule_subject", "subject_type", "subject_id"),)

    subject_type: Mapped[str] = mapped_column(String(16), nullable=False)
    #: A user id, a team id, or a role key — as text, so one column serves all three.
    subject_id: Mapped[str] = mapped_column(String(120), nullable=False)
    #: Resolved when the rule is made, so the list reads without a join.
    subject_label: Mapped[str] = mapped_column(String(200), nullable=False)
    effect: Mapped[str] = mapped_column(String(8), nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    note: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    def __repr__(self) -> str:
        return f"<AssistantAccessRule {self.effect} {self.subject_type}:{self.subject_id}>"


# ── record ─────────────────────────────────────────────────────────────


class RunStatus(StrEnum):
    RUNNING = "running"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    #: Parked on the browser: the model asked for something only the screen
    #: can do — press this, scroll there — and the turn waits for the report.
    AWAITING_CLIENT = "awaiting_client"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    #: Refused before the model was called: switched off, not released, over a cap.
    BLOCKED = "blocked"


#: A run that is still going, in whichever way: working, or parked on a
#: person's answer, or parked on the browser's report. One list, so that
#: "is this chat busy" is answered the same by the loop, the routes and the
#: analytics.
OPEN_STATUSES: tuple[RunStatus, ...] = (
    RunStatus.RUNNING,
    RunStatus.AWAITING_CONFIRMATION,
    RunStatus.AWAITING_CLIENT,
)


class EventKind(StrEnum):
    USER_MESSAGE = "user_message"
    ASSISTANT_TEXT = "assistant_text"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    CONFIRMATION_REQUESTED = "confirmation_requested"
    CONFIRMED = "confirmed"
    DECLINED = "declined"
    #: The turn asked the browser to do something on the screen, and what
    #: the browser said happened. Kept apart from tool_call/tool_result so the
    #: log reads honestly: nothing went through a route.
    CLIENT_ACTION_REQUESTED = "client_action_requested"
    CLIENT_ACTION_RESULT = "client_action_result"
    BLOCKED_BY_POLICY = "blocked_by_policy"
    MODEL_USAGE = "model_usage"
    CANCELLED = "cancelled"
    ERROR = "error"


class AssistantConversation(Base, UUIDPrimaryKey, Timestamped):
    __tablename__ = "assistant_conversations"
    __table_args__ = (
        Index("ix_assistant_conversations_subject", "subject_kind", "subject_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Taken from the first message; editable later.
    title: Mapped[str | None] = mapped_column(String(200))

    #: What this chat is *about*, when it was opened from somewhere specific —
    #: the box on a report page rather than the assistant's own screen.
    #:
    #: Kept as a loose (kind, id) pair with no foreign key on purpose. A
    #: conversation outlives what it was about: a report deleted last month
    #: should not take the conversation about it with it, and the chat still
    #: reads because what was said is in the messages. The kind is validated
    #: where conversations are opened, not here — ``subject.KINDS``.
    subject_kind: Mapped[str | None] = mapped_column(String(24))
    subject_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    #: How to name it to the model and on the screen: "Presales weekly, 1-7 Sep".
    #: Copied rather than joined, for the same reason the pair has no key.
    subject_label: Mapped[str | None] = mapped_column(String(200))
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(foreign_keys=[user_id], lazy="joined")

    def __repr__(self) -> str:
        return f"<AssistantConversation {self.id} {self.user_id}>"


class AssistantRun(Base, UUIDPrimaryKey, Timestamped):
    """One turn: a person's message, everything done in response, and the bill."""

    __tablename__ = "assistant_runs"
    __table_args__ = (
        Index("ix_assistant_runs_user_started", "user_id", "started_at"),
        Index("ix_assistant_runs_status", "status"),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("assistant_conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, default=RunStatus.RUNNING)
    model_key: Mapped[str] = mapped_column(String(64), nullable=False)
    reasoning_effort: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user_text: Mapped[str] = mapped_column(Text, nullable=False)
    answer_text: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)

    input_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    cached_input_tokens: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    reasoning_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), default=Decimal(0), server_default=text("0"), nullable=False
    )
    tool_calls: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    rounds: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))

    #: Set by a super admin; the loop reads it between rounds and stops.
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    #: A spoken conversation has had its usage reported by the browser. Set once
    #: and checked before recording, because the client sends this figure and a
    #: client can send it twice: a retried close, or the same session left open
    #: in a second tab, would otherwise bill the conversation over again.
    voice_usage_reported: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    #: The model's own items for this turn, in order, so a paused turn can be
    #: resumed exactly where it stopped. Never shown to a person.
    transcript: Mapped[list[Any]] = mapped_column(JSONB, default=list, nullable=False)
    #: The writes waiting on the person: ``[{call_id, tool_key, arguments, ...}]``.
    pending: Mapped[list[Any] | None] = mapped_column(JSONB)

    user: Mapped[User] = relationship(foreign_keys=[user_id], lazy="joined")
    events: Mapped[list[AssistantRunEvent]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="AssistantRunEvent.seq"
    )

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    def __repr__(self) -> str:
        return f"<AssistantRun {self.id} {self.status}>"


class AssistantRunEvent(Base, UUIDPrimaryKey, Timestamped):
    __tablename__ = "assistant_run_events"
    __table_args__ = (Index("ix_assistant_events_run_seq", "run_id", "seq"),)

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assistant_runs.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    tool_key: Mapped[str | None] = mapped_column(String(80), index=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    run: Mapped[AssistantRun] = relationship(back_populates="events")

    def __repr__(self) -> str:
        return f"<AssistantRunEvent {self.run_id} #{self.seq} {self.kind}>"


class AssistantMessage(Base, UUIDPrimaryKey, Timestamped):
    """What a person sees in the chat. The model's transcript lives on the run."""

    __tablename__ = "assistant_messages"
    __table_args__ = (Index("ix_assistant_messages_conv_seq", "conversation_id", "seq"),)

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("assistant_conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assistant_runs.id", ondelete="SET NULL")
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    #: "user" or "assistant".
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    #: For an assistant message: the tools it used, for the UI to show.
    tool_calls: Mapped[list[Any] | None] = mapped_column(JSONB)

    def __repr__(self) -> str:
        return f"<AssistantMessage {self.conversation_id} #{self.seq} {self.role}>"
